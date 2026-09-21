"""v0.4 Phase D: revision-guarded binary overwrite through the MCP surface."""

from __future__ import annotations

import base64
import dataclasses
import os
import stat
import threading

import pytest

from helpers import call_error, call_success, error_code, registry_for
from serverfs_mcp import mutations as mutation_module
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.mutations import MetadataPreservationError


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _server(workdir):
    wd = dataclasses.replace(
        workdir,
        read_only=False,
        policy=dataclasses.replace(
            workdir.policy,
            binary_transfer_enabled=True,
        ),
    )
    return create_server(Settings(), registry_for(wd))


def _revision(server, path: str) -> str:
    return call_success(server, "stat_file", {"workdir": "test", "path": path})["revision"]


def _overwrite(server, path: str, data: bytes, revision: str):
    return call_success(
        server,
        "upload_binary_file",
        {
            "workdir": "test",
            "path": path,
            "data_base64": _b64(data),
            "overwrite": True,
            "expected_revision": revision,
        },
    )


class TestOverwriteContract:
    def test_overwrite_requires_revision(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)

        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "x.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
            },
        )
        assert error_code(msg) == "EXPECTED_REVISION_REQUIRED"
        assert target.read_bytes() == b"OLD"

    def test_create_rejects_irrelevant_revision(self, workdir) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "new.bin",
                "data_base64": _b64(b"NEW"),
                "expected_revision": "v1:stale",
            },
        )
        assert error_code(msg) == "EXPECTED_REVISION_NOT_ALLOWED"
        assert not (workdir.container_path / "new.bin").exists()

    def test_correct_revision_replaces_exact_bytes(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)
        before = _revision(server, "x.bin")

        result = _overwrite(server, "x.bin", b"\x00NEW\xff", before)

        assert target.read_bytes() == b"\x00NEW\xff"
        assert result["created"] is False
        assert result["replaced"] is True
        assert result["revision_before"] == before
        assert result["revision"] != before
        assert _revision(server, "x.bin") == result["revision"]

    def test_stale_revision_fails_without_change(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)
        stale = _revision(server, "x.bin")
        target.write_bytes(b"EXTERNAL")

        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "x.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": stale,
            },
        )
        assert error_code(msg) == "REVISION_CONFLICT"
        assert target.read_bytes() == b"EXTERNAL"
        assert not list(workdir.container_path.glob(".serverfs-tmp-*"))

    def test_missing_target(self, workdir) -> None:
        server = _server(workdir)
        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "missing.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": "v1:missing",
            },
        )
        assert error_code(msg) == "PATH_NOT_FOUND"

    def test_directory_target_rejected(self, workdir) -> None:
        (workdir.container_path / "dir").mkdir()
        server = _server(workdir)
        rev = _revision(server, "dir")
        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "dir",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": rev,
            },
        )
        assert error_code(msg) == "NOT_A_FILE"

    def test_symlink_target_rejected(self, workdir) -> None:
        (workdir.container_path / "real.bin").write_bytes(b"OLD")
        os.symlink("real.bin", workdir.container_path / "link.bin")
        server = _server(workdir)
        rev = _revision(server, "link.bin")
        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "link.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": rev,
            },
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert (workdir.container_path / "real.bin").read_bytes() == b"OLD"


class TestOverwriteSafety:
    def test_multiple_hardlinks_rejected(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        peer = workdir.container_path / "peer.bin"
        target.write_bytes(b"OLD")
        os.link(target, peer)
        server = _server(workdir)
        before = _revision(server, "x.bin")

        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "x.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": before,
            },
        )
        assert error_code(msg) == "MULTIPLE_HARDLINKS_NOT_SUPPORTED"
        assert target.read_bytes() == b"OLD"
        assert peer.read_bytes() == b"OLD"

    def test_mode_and_ownership_preserved(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        os.chmod(target, 0o640)
        before_stat = target.stat()
        server = _server(workdir)
        before = _revision(server, "x.bin")

        _overwrite(server, "x.bin", b"NEW", before)
        after_stat = target.stat()

        assert stat.S_IMODE(after_stat.st_mode) == stat.S_IMODE(before_stat.st_mode)
        assert (after_stat.st_uid, after_stat.st_gid) == (before_stat.st_uid, before_stat.st_gid)

    def test_xattr_preserved_when_supported(self, workdir) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        try:
            os.setxattr(target, "user.serverfs_phase_d", b"kept")
        except OSError:
            pytest.skip("filesystem does not support writable user xattrs")

        server = _server(workdir)
        before = _revision(server, "x.bin")
        _overwrite(server, "x.bin", b"NEW", before)

        assert os.getxattr(target, "user.serverfs_phase_d") == b"kept"

    def test_metadata_failure_leaves_original_and_no_temp(self, workdir, monkeypatch) -> None:
        target = workdir.container_path / "x.bin"
        target.write_bytes(b"OLD")
        server = _server(workdir)
        before = _revision(server, "x.bin")

        def fail_metadata(*args, **kwargs):
            raise MetadataPreservationError("test failure")

        monkeypatch.setattr(mutation_module, "_preserve_metadata", fail_metadata)

        msg = call_error(
            server,
            "upload_binary_file",
            {
                "workdir": "test",
                "path": "x.bin",
                "data_base64": _b64(b"NEW"),
                "overwrite": True,
                "expected_revision": before,
            },
        )
        assert error_code(msg) == "METADATA_PRESERVATION_FAILED"
        assert target.read_bytes() == b"OLD"
        assert not list(workdir.container_path.glob(".serverfs-tmp-*"))

    def test_old_or_new_visibility_at_commit_seam(self, workdir, monkeypatch) -> None:
        target = workdir.container_path / "x.bin"
        old = b"O" * 4096
        new = b"N" * 4096
        target.write_bytes(old)
        server = _server(workdir)
        before = _revision(server, "x.bin")

        real_write_all = mutation_module._write_all
        temp_complete = threading.Event()
        release = threading.Event()

        def paused_write_all(fd: int, data: bytes) -> None:
            real_write_all(fd, data)
            temp_complete.set()
            assert release.wait(timeout=10)

        monkeypatch.setattr(mutation_module, "_write_all", paused_write_all)

        outcome: dict[str, object] = {}

        def worker() -> None:
            try:
                outcome["result"] = _overwrite(server, "x.bin", new, before)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert temp_complete.wait(timeout=10)
        assert target.read_bytes() == old
        release.set()
        thread.join(timeout=10)

        assert "error" not in outcome
        assert not thread.is_alive()
        assert target.read_bytes() == new
        assert target.read_bytes() in (old, new)
