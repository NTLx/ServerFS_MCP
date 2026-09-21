"""v0.4 Phase B: optional binary download through the public MCP surface."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import os

import pytest
from mcp.types import EmbeddedResource

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp import binary as binary_module
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.models import DownloadBinaryFileMetadata


def _binary_workdir(workdir, *, enabled: bool = True, max_bytes: int = 8_388_608):
    return dataclasses.replace(
        workdir,
        policy=dataclasses.replace(
            workdir.policy,
            binary_transfer_enabled=enabled,
            max_binary_transfer_bytes=max_bytes,
        ),
    )


def _binary_server(workdir, *, max_bytes: int = 8_388_608):
    wd = _binary_workdir(workdir, max_bytes=max_bytes)
    return create_server(Settings(), registry_for(wd))


def _tool_names(server) -> set[str]:
    async def _list() -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    return asyncio.run(_list())


def _tool_output_schema(server, name: str) -> dict:
    async def _get() -> dict:
        for tool in await server.list_tools():
            if tool.name == name:
                assert tool.output_schema is not None
                return tool.output_schema
        raise AssertionError(f"{name} not registered")

    return asyncio.run(_get())


def _download(server, *, workdir: str = "test", path: str):
    async def _call():
        result = await server.call_tool(
            "download_binary_file",
            {"workdir": workdir, "path": path},
        )
        assert not result.is_error, result
        return result

    return asyncio.run(_call())


def _blob_bytes(result) -> bytes:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, EmbeddedResource)
    return base64.b64decode(block.resource.blob, validate=True)


class TestBinaryToolRegistration:
    def test_tool_absent_when_disabled_everywhere(self, workdir) -> None:
        server = create_server(Settings(), registry_for(workdir))
        assert "download_binary_file" not in _tool_names(server)

    def test_tool_present_when_any_workdir_enables_binary(self, workdir) -> None:
        server = _binary_server(workdir)
        assert "download_binary_file" in _tool_names(server)

    def test_download_output_schema_is_exact_metadata_schema(self, workdir) -> None:
        schema = _tool_output_schema(_binary_server(workdir), "download_binary_file")
        assert schema == DownloadBinaryFileMetadata.model_json_schema()
        assert set(schema["properties"]) == {
            "workdir",
            "path",
            "size",
            "mime_type",
            "sha256",
            "revision",
        }
        assert set(schema["required"]) == {
            "workdir",
            "path",
            "size",
            "mime_type",
            "sha256",
            "revision",
        }

    def test_disabled_selected_workdir_fails_even_when_tool_is_registered(self, workdir) -> None:
        disabled = dataclasses.replace(workdir, alias="disabled")
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
            "download_binary_file",
            {"workdir": "disabled", "path": "x.bin"},
        )
        assert error_code(msg) == "BINARY_TRANSFER_DISABLED"


class TestBinaryDownloadResult:
    def test_exact_bytes_resource_and_metadata(self, workdir) -> None:
        raw = b"\x89PNG\r\n\x1a\n\x00payload\xff"
        (workdir.container_path / "image.png").write_bytes(raw)
        server = _binary_server(workdir)

        result = _download(server, path="image.png")
        metadata = result.structured_content

        assert _blob_bytes(result) == raw
        assert metadata == {
            "workdir": "test",
            "path": "image.png",
            "size": len(raw),
            "mime_type": "image/png",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "revision": metadata["revision"],
        }
        assert metadata["revision"].startswith("v1:")
        block = result.content[0]
        assert block.resource.mime_type == "image/png"
        assert str(block.resource.uri) == "serverfs://test/image.png"

        stat = call_success(server, "stat_file", {"workdir": "test", "path": "image.png"})
        assert metadata["revision"] == stat["revision"]

    def test_empty_file(self, workdir) -> None:
        (workdir.container_path / "empty.bin").write_bytes(b"")
        server = _binary_server(workdir)

        result = _download(server, path="empty.bin")
        assert _blob_bytes(result) == b""
        assert result.structured_content["size"] == 0
        assert result.structured_content["sha256"] == hashlib.sha256(b"").hexdigest()

    def test_utf8_text_is_allowed_through_raw_channel(self, workdir) -> None:
        raw = "第一行\nsecond\n".encode()
        (workdir.container_path / "text.txt").write_bytes(raw)
        server = _binary_server(workdir)

        result = _download(server, path="text.txt")
        assert _blob_bytes(result) == raw
        assert result.structured_content["mime_type"] == "text/plain"

    def test_unknown_extension_uses_octet_stream(self, workdir) -> None:
        raw = b"\x00\x01"
        (workdir.container_path / "artifact.unknownext").write_bytes(raw)
        server = _binary_server(workdir)

        result = _download(server, path="artifact.unknownext")
        assert result.structured_content["mime_type"] == "application/octet-stream"

    def test_uri_percent_encodes_path_components(self, workdir) -> None:
        sub = workdir.container_path / "folder"
        sub.mkdir()
        (sub / "a b.bin").write_bytes(b"x")
        server = _binary_server(workdir)

        result = _download(server, path="folder/a b.bin")
        block = result.content[0]
        assert str(block.resource.uri) == "serverfs://test/folder/a%20b.bin"


class TestBinaryDownloadLimitsAndConsistency:
    def test_effective_workdir_size_limit(self, workdir) -> None:
        (workdir.container_path / "large.bin").write_bytes(b"12345")
        server = _binary_server(workdir, max_bytes=4)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "large.bin"},
        )
        assert error_code(msg) == "BINARY_FILE_TOO_LARGE"

    def test_file_changed_during_read_is_rejected(self, workdir, monkeypatch) -> None:
        target = workdir.container_path / "changing.bin"
        target.write_bytes(b"abcdef")
        server = _binary_server(workdir)
        real_read = binary_module.os.read
        changed = False

        def mutating_read(fd: int, size: int) -> bytes:
            nonlocal changed
            data = real_read(fd, size)
            if data and not changed:
                changed = True
                with target.open("ab") as fh:
                    fh.write(b"!")
                    fh.flush()
                    os.fsync(fh.fileno())
            return data

        monkeypatch.setattr(binary_module.os, "read", mutating_read)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "changing.bin"},
        )
        assert error_code(msg) == "FILE_CHANGED_DURING_READ"


class TestBinaryDownloadPolicyParity:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (".notes", "HIDDEN_PATH_NOT_ALLOWED"),
            ("id_rsa", "DENIED_PATH"),
            (".serverfs-tmp-probe", "RESERVED_PATH"),
        ],
    )
    def test_hidden_deny_and_reserved_policy(self, workdir, path, expected) -> None:
        (workdir.container_path / path).write_bytes(b"secret")
        server = _binary_server(workdir)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": path},
        )
        assert error_code(msg) == expected

    def test_extra_deny_policy(self, workdir) -> None:
        target = workdir.container_path / "blocked.bin"
        target.write_bytes(b"x")
        wd = _binary_workdir(workdir)
        wd = dataclasses.replace(
            wd,
            policy=dataclasses.replace(
                wd.policy,
                extra_deny_globs=("blocked.bin",),
            ),
        )
        server = create_server(Settings(), registry_for(wd))

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "blocked.bin"},
        )
        assert error_code(msg) == "DENIED_PATH"

    def test_final_symlink_rejected(self, workdir) -> None:
        (workdir.container_path / "real.bin").write_bytes(b"x")
        os.symlink("real.bin", workdir.container_path / "link.bin")
        server = _binary_server(workdir)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "link.bin"},
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"

    def test_parent_symlink_rejected(self, workdir) -> None:
        outside = workdir.container_path.parent / "outside"
        outside.mkdir()
        (outside / "x.bin").write_bytes(b"x")
        os.symlink(outside, workdir.container_path / "linkdir")
        server = _binary_server(workdir)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "linkdir/x.bin"},
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"

    def test_directory_rejected(self, workdir) -> None:
        (workdir.container_path / "dir").mkdir()
        server = _binary_server(workdir)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "dir"},
        )
        assert error_code(msg) == "NOT_A_FILE"

    def test_fifo_rejected_without_blocking(self, workdir) -> None:
        os.mkfifo(workdir.container_path / "pipe")
        server = _binary_server(workdir)

        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "pipe"},
        )
        assert error_code(msg) == "UNSUPPORTED_FILE_TYPE"

    def test_missing_file(self, workdir) -> None:
        server = _binary_server(workdir)
        msg = call_error(
            server,
            "download_binary_file",
            {"workdir": "test", "path": "missing.bin"},
        )
        assert error_code(msg) == "PATH_NOT_FOUND"
