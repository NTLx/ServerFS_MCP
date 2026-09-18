"""§96: create_text_file — the target must not exist, ever."""

from __future__ import annotations

import os
import socket
import stat as stat_module

import pytest

from helpers import call_error, call_success, error_code, make_server

WRITE_ARGS = {"workdir": "test", "path": "new.txt", "content": "hello\n"}


class TestCreateSuccess:
    def test_creates_exact_content(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        data = call_success(srv, "create_text_file", WRITE_ARGS)
        assert data["created"] is True
        assert data["bytes_written"] == len(b"hello\n")
        assert data["revision"].startswith("v1:")
        assert (workdir.container_path / "new.txt").read_bytes() == b"hello\n"

    def test_creates_inside_existing_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "docs")
        call_success(
            srv,
            "create_text_file",
            {"workdir": "test", "path": "docs/design.md", "content": "# Design\n"},
        )
        assert (workdir.container_path / "docs" / "design.md").read_text() == "# Design\n"

    def test_empty_content_is_allowed(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        data = call_success(
            srv, "create_text_file", {"workdir": "test", "path": "empty.txt", "content": ""}
        )
        assert data["bytes_written"] == 0
        assert (workdir.container_path / "empty.txt").read_bytes() == b""

    def test_content_is_written_verbatim(self, workdir) -> None:
        """No newline normalization, no trimming, no added final newline."""
        srv = make_server(workdir, read_write_access=True)
        content = "  no trailing newline\r\nCRLF kept\t trailing spaces  "
        data = call_success(
            srv, "create_text_file", {"workdir": "test", "path": "raw.txt", "content": content}
        )
        # byte-level: read_text() would normalize CRLF away in the test itself
        assert (workdir.container_path / "raw.txt").read_bytes() == content.encode()
        assert data["bytes_written"] == len(content.encode())

    def test_unicode_content(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        content = "中文标题\nemoji: ✅\n"
        data = call_success(
            srv, "create_text_file", {"workdir": "test", "path": "u.txt", "content": content}
        )
        assert data["bytes_written"] == len(content.encode())
        assert (workdir.container_path / "u.txt").read_text() == content

    def test_content_has_no_bom(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_text_file", WRITE_ARGS)
        assert (workdir.container_path / "new.txt").read_bytes() == b"hello\n"

    def test_created_file_is_regular_and_executable_free(self, workdir) -> None:
        """mode 0o666 subject to umask: owner read/write, never executable."""
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_text_file", WRITE_ARGS)
        mode = stat_module.S_IMODE(os.lstat(workdir.container_path / "new.txt").st_mode)
        assert mode & 0o600 == 0o600
        assert mode & 0o111 == 0

    def test_created_file_is_readable_through_the_read_channels(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_text_file", WRITE_ARGS)
        read = call_success(srv, "read_text_file", {"workdir": "test", "path": "new.txt"})
        assert read["content"] == "hello\n"
        listed = call_success(srv, "list_directory", {"workdir": "test", "path": ""})
        assert "new.txt" in [e["name"] for e in listed["entries"]]

    def test_no_temp_files_remain(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_text_file", WRITE_ARGS)
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["new.txt"]


class TestCreateConflict:
    def test_existing_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "new.txt").write_text("original\n")
        msg = call_error(srv, "create_text_file", WRITE_ARGS)
        assert error_code(msg) == "PATH_ALREADY_EXISTS"
        assert (workdir.container_path / "new.txt").read_text() == "original\n"

    def test_existing_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "new.txt")
        assert error_code(call_error(srv, "create_text_file", WRITE_ARGS)) == "PATH_ALREADY_EXISTS"
        assert (workdir.container_path / "new.txt").is_dir()

    def test_existing_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "target.txt").write_text("target\n")
        os.symlink("target.txt", workdir.container_path / "new.txt")
        assert error_code(call_error(srv, "create_text_file", WRITE_ARGS)) == "PATH_ALREADY_EXISTS"
        assert (workdir.container_path / "new.txt").is_symlink()
        assert (workdir.container_path / "target.txt").read_text() == "target\n"

    def test_existing_fifo(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkfifo(workdir.container_path / "new.txt")
        assert error_code(call_error(srv, "create_text_file", WRITE_ARGS)) == "PATH_ALREADY_EXISTS"

    def test_existing_socket(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(workdir.container_path / "new.txt"))
        finally:
            sock.close()
        assert error_code(call_error(srv, "create_text_file", WRITE_ARGS)) == "PATH_ALREADY_EXISTS"

    def test_read_only_workdir(self, workdir) -> None:
        srv = make_server(workdir)
        assert error_code(call_error(srv, "create_text_file", WRITE_ARGS)) == "WORKDIR_READ_ONLY"
        assert not (workdir.container_path / "new.txt").exists()

    def test_parent_missing(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "no/dir/x.txt", "content": "x"}
        )
        assert error_code(msg) == "PARENT_NOT_FOUND"

    def test_parent_is_a_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "afile").write_text("x")
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "afile/x.txt", "content": "x"}
        )
        assert error_code(msg) == "NOT_A_DIRECTORY"

    def test_parent_is_a_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        os.symlink("real", workdir.container_path / "link")
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "link/x.txt", "content": "x"}
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert not (workdir.container_path / "real" / "x.txt").exists()

    def test_symlink_escaping_the_workdir(self, workdir, tmp_path) -> None:
        srv = make_server(workdir, read_write_access=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workdir.container_path / "escape")
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "escape/x.txt", "content": "x"}
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert list(outside.iterdir()) == []

    def test_workdir_root(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_text_file", {"workdir": "test", "path": "", "content": "x"})
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
    def test_parent_without_read_permission(self, workdir) -> None:
        """The FD walk needs read permission on every parent component, so a
        write-only directory fails closed rather than being traversed."""
        srv = make_server(workdir, read_write_access=True)
        locked = workdir.container_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o300)
        try:
            msg = call_error(
                srv, "create_text_file", {"workdir": "test", "path": "locked/x.txt", "content": "x"}
            )
            assert error_code(msg) == "ACCESS_DENIED"
        finally:
            os.chmod(locked, 0o700)

    @pytest.mark.parametrize("path", ["../escape.txt", "/etc/escape.txt", "~/escape.txt"])
    def test_paths_outside_the_workdir(self, workdir, path) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_text_file", {"workdir": "test", "path": path, "content": "x"})
        assert error_code(msg) == "PATH_OUTSIDE_WORKDIR"

    def test_nul_in_path(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "a\x00b", "content": "x"}
        )
        assert error_code(msg) == "ACCESS_DENIED"

    def test_unknown_workdir(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "nope", "path": "x.txt", "content": "x"}
        )
        assert error_code(msg) == "WORKDIR_NOT_FOUND"


class TestCreatePolicy:
    def test_hidden_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": ".secret", "content": "x"}
        )
        assert error_code(msg) == "HIDDEN_PATH_NOT_ALLOWED"
        assert not (workdir.container_path / ".secret").exists()

    @pytest.mark.parametrize("path", [".env", "app.pem", "keys/server.key", ".ssh/config"])
    def test_default_deny_blocked(self, workdir, path) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        msg = call_error(srv, "create_text_file", {"workdir": "test", "path": path, "content": "x"})
        assert error_code(msg) == "DENIED_PATH"

    def test_hidden_allowed_creates_normally(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        call_success(
            srv, "create_text_file", {"workdir": "test", "path": ".notes", "content": "x\n"}
        )
        assert (workdir.container_path / ".notes").read_text() == "x\n"

    def test_default_deny_disabled_allows_credential_names(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        call_success(
            srv, "create_text_file", {"workdir": "test", "path": ".env", "content": "A=1\n"}
        )
        assert (workdir.container_path / ".env").read_text() == "A=1\n"

    def test_extra_deny_applies_with_default_deny_disabled(self, workdir) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("*.secret",),
        )
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "x.secret", "content": "x"}
        )
        assert error_code(msg) == "DENIED_PATH"

    def test_extra_deny_directory_glob(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, extra_deny_globs=("internal/**",))
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "internal/x.txt", "content": "x"}
        )
        assert error_code(msg) == "DENIED_PATH"

    @pytest.mark.parametrize(
        "path", [".serverfs-tmp-x", ".serverfs-tmp-abc123", "d/.serverfs-tmp-y"]
    )
    def test_reserved_namespace_blocked(self, workdir, path) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        msg = call_error(srv, "create_text_file", {"workdir": "test", "path": path, "content": "x"})
        assert error_code(msg) == "RESERVED_PATH"


class TestCreateLimits:
    def test_content_over_the_write_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=64)
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "big.txt", "content": "x" * 65}
        )
        assert error_code(msg) == "WRITE_TOO_LARGE"
        assert not (workdir.container_path / "big.txt").exists()

    def test_content_at_the_write_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=64)
        call_success(
            srv, "create_text_file", {"workdir": "test", "path": "big.txt", "content": "x" * 64}
        )
        assert (workdir.container_path / "big.txt").stat().st_size == 64

    def test_nul_byte_in_content(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "test", "path": "bin.txt", "content": "a\x00b"}
        )
        assert error_code(msg) == "BINARY_CONTENT_NOT_ALLOWED"
        assert not (workdir.container_path / "bin.txt").exists()


class TestCreateAtomicity:
    def test_failed_create_leaves_no_trace(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "new.txt").write_text("original\n")
        call_error(srv, "create_text_file", WRITE_ARGS)
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["new.txt"]
        assert (workdir.container_path / "new.txt").read_text() == "original\n"

    def test_link_failure_leaves_no_final_file_and_no_temp(self, workdir, monkeypatch) -> None:
        """A failed publish must not leave a partial file behind."""
        srv = make_server(workdir, read_write_access=True)

        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "link", boom)
        msg = call_error(srv, "create_text_file", WRITE_ARGS)
        assert error_code(msg) == "MUTATION_IO_ERROR"
        assert list(workdir.container_path.iterdir()) == []

    def test_write_failure_leaves_no_final_file_and_no_temp(self, workdir, monkeypatch) -> None:
        srv = make_server(workdir, read_write_access=True)

        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "write", boom)
        msg = call_error(srv, "create_text_file", WRITE_ARGS)
        assert error_code(msg) == "MUTATION_IO_ERROR"
        assert list(workdir.container_path.iterdir()) == []

    def test_concurrent_create_of_the_same_path(self, workdir) -> None:
        """Only one of two racing creates may win; the other must see the
        winner, not overwrite it."""
        from helpers import call_concurrently, error_codes, outcomes

        srv = make_server(workdir, read_write_access=True)
        results = call_concurrently(
            srv,
            [("create_text_file", WRITE_ARGS), ("create_text_file", WRITE_ARGS)],
        )
        assert sorted(outcomes(results)) == ["err", "ok"]
        assert error_codes(results) == ["PATH_ALREADY_EXISTS"]
        assert (workdir.container_path / "new.txt").read_text() == "hello\n"
