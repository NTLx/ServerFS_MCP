"""v0.4 Phase C: create-only binary upload through the public MCP surface."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import fcntl
import hashlib
import os

import pytest

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server


def _binary_workdir(workdir, *, read_only: bool = False, max_bytes: int = 8_388_608):
    return dataclasses.replace(
        workdir,
        read_only=read_only,
        policy=dataclasses.replace(
            workdir.policy,
            binary_transfer_enabled=True,
            max_binary_transfer_bytes=max_bytes,
        ),
    )


def _server(workdir, *, read_only: bool = False, max_bytes: int = 8_388_608, settings=None):
    wd = _binary_workdir(workdir, read_only=read_only, max_bytes=max_bytes)
    return create_server(settings or Settings(), registry_for(wd))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _tool_names(server) -> set[str]:
    async def _list() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_list())


def _tool_schema(server, name: str) -> dict:
    async def _get() -> dict:
        for tool in await server.list_tools():
            if tool.name == name:
                return tool.input_schema
        raise AssertionError(f"{name} not registered")

    return asyncio.run(_get())


class TestBinaryUploadRegistration:
    def test_binary_enabled_surface_has_download_and_upload(self, workdir) -> None:
        server = _server(workdir)
        names = _tool_names(server)
        assert "download_binary_file" in names
        assert "upload_binary_file" in names
        assert len(names) == 13

    def test_upload_schema_exposes_create_only_overwrite_default(self, workdir) -> None:
        schema = _tool_schema(_server(workdir), "upload_binary_file")
        assert set(schema["properties"]) == {"workdir", "path", "data_base64", "overwrite"}
        assert schema["properties"]["overwrite"]["default"] is False
        assert "overwrite" not in schema.get("required", [])

    def test_binary_disabled_surface_has_neither_binary_tool(self, workdir) -> None:
        server = create_server(Settings(), registry_for(workdir))
        names = _tool_names(server)
        assert "download_binary_file" not in names
        assert "upload_binary_file" not in names
        assert len(names) == 11


class TestBinaryUploadCreate:
    def test_exact_bytes_sha_and_revision(self, workdir) -> None:
        raw = b"\x00\x01payload\xff\n"
        server = _server(workdir)

        result = call_success(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "artifact.bin", "data_base64": _b64(raw)},
        )

        assert (workdir.container_path / "artifact.bin").read_bytes() == raw
        assert result["created"] is True
        assert result["bytes_written"] == len(raw)
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["revision"].startswith("v1:")
        stat = call_success(server, "stat_file", {"workdir": "test", "path": "artifact.bin"})
        assert stat["revision"] == result["revision"]

    def test_empty_payload(self, workdir) -> None:
        server = _server(workdir)
        result = call_success(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "empty.bin", "data_base64": ""},
        )
        assert (workdir.container_path / "empty.bin").read_bytes() == b""
        assert result["bytes_written"] == 0
        assert result["sha256"] == hashlib.sha256(b"").hexdigest()

    @pytest.mark.parametrize("payload", ["***", "YQ=", "Y Q==", "你好"])
    def test_malformed_base64_rejected(self, workdir, payload) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "bad.bin", "data_base64": payload},
        )
        assert error_code(msg) == "INVALID_BASE64"
        assert not (workdir.container_path / "bad.bin").exists()

    def test_encoded_length_precheck(self, workdir) -> None:
        server = _server(workdir, max_bytes=3)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "big.bin", "data_base64": "A" * 8},
        )
        assert error_code(msg) == "BINARY_PAYLOAD_TOO_LARGE"
        assert not (workdir.container_path / "big.bin").exists()

    def test_decoded_size_limit(self, workdir) -> None:
        server = _server(workdir, max_bytes=4)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "big.bin", "data_base64": _b64(b"12345")},
        )
        assert error_code(msg) == "BINARY_PAYLOAD_TOO_LARGE"
        assert not (workdir.container_path / "big.bin").exists()

    def test_overwrite_true_fails_closed_without_touching_target(self, workdir) -> None:
        target = workdir.container_path / "keep.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)

        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "keep.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
            },
        )
        assert error_code(msg) == "OVERWRITE_NOT_ALLOWED"
        assert target.read_bytes() == b"OLD"
        assert not list(workdir.container_path.glob(".serverfs-tmp-*"))

    def test_existing_regular_file_is_never_overwritten(self, workdir) -> None:
        target = workdir.container_path / "exists.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)

        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "exists.bin", "data_base64": _b64(b"NEW")},
        )
        assert error_code(msg) == "PATH_ALREADY_EXISTS"
        assert target.read_bytes() == b"OLD"
        assert not list(workdir.container_path.glob(".serverfs-tmp-*"))

    @pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
    def test_any_existing_target_blocks_create(self, workdir, kind) -> None:
        target = workdir.container_path / "occupied"
        if kind == "directory":
            target.mkdir()
        elif kind == "symlink":
            (workdir.container_path / "real").write_bytes(b"x")
            os.symlink("real", target)
        else:
            os.mkfifo(target)
        server = _server(workdir)

        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "occupied", "data_base64": _b64(b"NEW")},
        )
        assert error_code(msg) == "PATH_ALREADY_EXISTS"

    def test_symlink_parent_rejected(self, workdir) -> None:
        outside = workdir.container_path.parent / "outside-upload"
        outside.mkdir()
        os.symlink(outside, workdir.container_path / "linkdir")
        server = _server(workdir)

        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "linkdir/x.bin", "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert not (outside / "x.bin").exists()

    def test_missing_parent(self, workdir) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "missing/x.bin", "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == "PARENT_NOT_FOUND"


class TestBinaryUploadAuthorization:
    def test_read_only_precedes_binary_and_path_policy(self, workdir) -> None:
        disabled_policy = dataclasses.replace(workdir.policy, binary_transfer_enabled=False)
        wd = dataclasses.replace(workdir, read_only=True, policy=disabled_policy)
        enabled = dataclasses.replace(
            _binary_workdir(workdir),
            slot=2,
            alias="enabled",
            container_path=workdir.container_path.parent / "02",
        )
        enabled.container_path.mkdir(exist_ok=True)
        server = create_server(Settings(), registry_for(wd, enabled))

        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": ".env", "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"

    def test_binary_disabled_selected_workdir(self, workdir) -> None:
        disabled = dataclasses.replace(workdir, read_only=False)
        enabled = dataclasses.replace(
            _binary_workdir(workdir),
            slot=2,
            alias="enabled",
            container_path=workdir.container_path.parent / "02",
        )
        enabled.container_path.mkdir(exist_ok=True)
        server = create_server(Settings(), registry_for(disabled, enabled))

        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "x.bin", "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == "BINARY_TRANSFER_DISABLED"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (".notes", "HIDDEN_PATH_NOT_ALLOWED"),
            ("id_rsa", "DENIED_PATH"),
            (".serverfs-tmp-probe", "RESERVED_PATH"),
        ],
    )
    def test_path_policy_parity(self, workdir, path, expected) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": path, "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == expected

    def test_root_mutation_rejected(self, workdir) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {"workdir": "test", "path": "", "data_base64": _b64(b"x")},
        )
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"


class TestBinaryUploadWriterLease:
    def test_active_agent_writer_blocks_upload(self, workdir, tmp_path) -> None:
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "01.lock"
        lock_path.touch(mode=0o640)
        settings = Settings(
            agent_bridge_enabled=True,
            agent_lock_dir=str(lock_dir),
        )
        server = _server(workdir, settings=settings)

        fd = os.open(lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            msg = call_error(
                server,
                "upload_binary_file",
                {"workdir": "test", "path": "blocked.bin", "data_base64": _b64(b"x")},
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert error_code(msg) == "WORKDIR_BUSY"
        assert not (workdir.container_path / "blocked.bin").exists()
