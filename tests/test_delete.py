"""§98: delete_file — revision-guarded, permanent, never a directory."""

from __future__ import annotations

import os
import socket

import pytest

from helpers import call_error, call_success, error_code, make_server


def seed(workdir, name: str, data: bytes = b"content\n") -> None:
    (workdir.container_path / name).write_bytes(data)


def revision_of(srv, path: str) -> str:
    return call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]


def delete(srv, path: str, revision: str):
    return call_success(
        srv, "delete_file", {"workdir": "test", "path": path, "expected_revision": revision}
    )


class TestDeleteSuccess:
    def test_deletes_text_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "obsolete.txt", b"goodbye\n")
        revision = revision_of(srv, "obsolete.txt")
        data = delete(srv, "obsolete.txt", revision)
        assert data["deleted"] is True
        assert data["bytes_deleted"] == len(b"goodbye\n")
        assert data["revision_deleted"] == revision
        assert not (workdir.container_path / "obsolete.txt").exists()

    def test_deletes_binary_file(self, workdir) -> None:
        """Deletion is not limited to text: any regular file can go."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "image.png", b"\x89PNG\r\n\x1a\n\x00\x00binary")
        delete(srv, "image.png", revision_of(srv, "image.png"))
        assert not (workdir.container_path / "image.png").exists()

    def test_delete_has_no_size_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=16)
        seed(workdir, "big.bin", b"x" * 4096)
        delete(srv, "big.bin", revision_of(srv, "big.bin"))
        assert not (workdir.container_path / "big.bin").exists()

    def test_delete_inside_a_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "sub")
        seed(workdir, "sub/a.txt")
        delete(srv, "sub/a.txt", revision_of(srv, "sub/a.txt"))
        assert list((workdir.container_path / "sub").iterdir()) == []

    def test_leaves_no_temp_files(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt")
        delete(srv, "a.txt", revision_of(srv, "a.txt"))
        assert list(workdir.container_path.iterdir()) == []

    def test_one_hardlink_name_can_be_removed(self, workdir) -> None:
        """Only *editing* a hardlinked file is refused; removing one of its
        names is a plain unlink."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", b"shared\n")
        os.link(workdir.container_path / "a.txt", workdir.container_path / "b.txt")
        delete(srv, "a.txt", revision_of(srv, "a.txt"))
        assert not (workdir.container_path / "a.txt").exists()
        assert (workdir.container_path / "b.txt").read_text() == "shared\n"


class TestDeleteRevision:
    def test_stale_revision_is_refused(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", b"one\n")
        stale = revision_of(srv, "a.txt")
        (workdir.container_path / "a.txt").write_text("changed\n")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "a.txt", "expected_revision": stale}
        )
        assert error_code(msg) == "REVISION_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "changed\n"

    def test_revision_from_read_is_accepted(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", b"one\n")
        revision = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})[
            "revision"
        ]
        assert delete(srv, "a.txt", revision)["deleted"] is True

    def test_revision_is_required(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt")
        msg = call_error(srv, "delete_file", {"workdir": "test", "path": "a.txt"})
        assert "expected_revision" in msg


class TestDeleteTargetRequirements:
    def test_missing_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv,
            "delete_file",
            {"workdir": "test", "path": "ghost.txt", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "PATH_NOT_FOUND"

    def test_directory_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "sub")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "sub", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "NOT_A_FILE"
        assert (workdir.container_path / "sub").is_dir()

    def test_symlink_target(self, workdir) -> None:
        """A symlink is never followed, and never removed by delete_file."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "real.txt", b"target\n")
        os.symlink("real.txt", workdir.container_path / "lnk")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "lnk", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert (workdir.container_path / "lnk").is_symlink()
        assert (workdir.container_path / "real.txt").read_text() == "target\n"

    def test_fifo_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkfifo(workdir.container_path / "pipe")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "pipe", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "UNSUPPORTED_FILE_TYPE"
        assert (workdir.container_path / "pipe").exists()

    def test_socket_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(workdir.container_path / "asock"))
        finally:
            sock.close()
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "asock", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "UNSUPPORTED_FILE_TYPE"

    def test_parent_is_a_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        seed(workdir, "real/a.txt")
        os.symlink("real", workdir.container_path / "link")
        msg = call_error(
            srv,
            "delete_file",
            {"workdir": "test", "path": "link/a.txt", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert (workdir.container_path / "real" / "a.txt").exists()

    def test_read_only_workdir(self, workdir) -> None:
        srv = make_server(workdir)
        seed(workdir, "a.txt")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "a.txt", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"
        assert (workdir.container_path / "a.txt").exists()

    def test_workdir_root(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
    def test_file_without_read_permission_is_refused(self, workdir) -> None:
        """§51: unlink permission alone must not let the agent delete a file
        it cannot read."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "private.txt", b"secret\n")
        revision = revision_of(srv, "private.txt")
        os.chmod(workdir.container_path / "private.txt", 0o000)
        try:
            msg = call_error(
                srv,
                "delete_file",
                {"workdir": "test", "path": "private.txt", "expected_revision": revision},
            )
            assert error_code(msg) == "ACCESS_DENIED"
            assert (workdir.container_path / "private.txt").exists()
        finally:
            os.chmod(workdir.container_path / "private.txt", 0o600)


class TestDeletePolicy:
    def test_hidden_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, ".notes")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": ".notes", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "HIDDEN_PATH_NOT_ALLOWED"
        assert (workdir.container_path / ".notes").exists()

    def test_hidden_allowed(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        seed(workdir, ".notes")
        delete(srv, ".notes", revision_of(srv, ".notes"))
        assert not (workdir.container_path / ".notes").exists()

    def test_default_deny_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        seed(workdir, ".env")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": ".env", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "DENIED_PATH"
        assert (workdir.container_path / ".env").exists()

    def test_default_deny_disabled_allows_delete(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        seed(workdir, ".env")
        delete(srv, ".env", revision_of(srv, ".env"))
        assert not (workdir.container_path / ".env").exists()

    def test_extra_deny_still_applies(self, workdir) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("*.secret",),
        )
        seed(workdir, "a.secret")
        msg = call_error(
            srv, "delete_file", {"workdir": "test", "path": "a.secret", "expected_revision": "v1:x"}
        )
        assert error_code(msg) == "DENIED_PATH"

    def test_reserved_namespace_blocked(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        seed(workdir, ".serverfs-tmp-abc")
        msg = call_error(
            srv,
            "delete_file",
            {"workdir": "test", "path": ".serverfs-tmp-abc", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "RESERVED_PATH"
        assert (workdir.container_path / ".serverfs-tmp-abc").exists()
