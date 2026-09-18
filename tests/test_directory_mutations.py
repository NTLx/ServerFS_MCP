"""§99: create_directory / delete_directory — one level, empty only."""

from __future__ import annotations

import os
import stat as stat_module

import pytest

from helpers import call_error, call_success, error_code, make_server


def revision_of(srv, path: str) -> str:
    return call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]


class TestCreateDirectorySuccess:
    def test_creates_one_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        data = call_success(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert data["created"] is True
        assert data["revision"].startswith("v1:")
        assert (workdir.container_path / "docs").is_dir()

    def test_creates_inside_an_existing_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "docs")
        call_success(srv, "create_directory", {"workdir": "test", "path": "docs/v0.2"})
        assert (workdir.container_path / "docs" / "v0.2").is_dir()

    def test_revision_matches_stat(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        created = call_success(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert created["revision"] == revision_of(srv, "docs")

    def test_mode_is_umask_based(self, workdir) -> None:
        """mode 0o777 subject to umask: writable, no setuid/setgid/sticky."""
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_directory", {"workdir": "test", "path": "docs"})
        mode = stat_module.S_IMODE(os.lstat(workdir.container_path / "docs").st_mode)
        assert mode & 0o700 == 0o700
        assert mode & 0o7000 == 0

    def test_visible_through_read_channels(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_directory", {"workdir": "test", "path": "docs"})
        entries = call_success(srv, "list_directory", {"workdir": "test", "path": ""})["entries"]
        assert next(e for e in entries if e["name"] == "docs")["type"] == "directory"

    def test_no_temp_files(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["docs"]


class TestCreateDirectoryRules:
    def test_parent_missing(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "no/docs"})
        assert error_code(msg) == "PARENT_NOT_FOUND"
        assert list(workdir.container_path.iterdir()) == []

    def test_is_not_recursive(self, workdir) -> None:
        """mkdir -p is deliberately absent: every level is its own call."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "docs")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "docs/releases/v0.2"})
        assert error_code(msg) == "PARENT_NOT_FOUND"
        assert list((workdir.container_path / "docs").iterdir()) == []

    def test_existing_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "docs")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert error_code(msg) == "PATH_ALREADY_EXISTS"

    def test_existing_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "docs").write_text("x")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert error_code(msg) == "PATH_ALREADY_EXISTS"
        assert (workdir.container_path / "docs").is_file()

    def test_existing_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        os.symlink("real", workdir.container_path / "docs")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert error_code(msg) == "PATH_ALREADY_EXISTS"
        assert (workdir.container_path / "docs").is_symlink()

    def test_parent_is_a_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "afile").write_text("x")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "afile/docs"})
        assert error_code(msg) == "NOT_A_DIRECTORY"

    def test_parent_is_a_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        os.symlink("real", workdir.container_path / "link")
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "link/docs"})
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert list((workdir.container_path / "real").iterdir()) == []

    def test_workdir_root(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": ""})
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "."})
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"

    def test_read_only_workdir(self, workdir) -> None:
        srv = make_server(workdir)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "docs"})
        assert error_code(msg) == "WORKDIR_READ_ONLY"
        assert not (workdir.container_path / "docs").exists()

    @pytest.mark.parametrize("path", ["../escape", "/tmp/escape"])
    def test_paths_outside_the_workdir(self, workdir, path) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": path})
        assert error_code(msg) == "PATH_OUTSIDE_WORKDIR"


class TestCreateDirectoryPolicy:
    def test_hidden_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": ".cache"})
        assert error_code(msg) == "HIDDEN_PATH_NOT_ALLOWED"

    def test_hidden_allowed(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        call_success(srv, "create_directory", {"workdir": "test", "path": ".cache"})
        assert (workdir.container_path / ".cache").is_dir()

    @pytest.mark.parametrize("path", [".ssh", ".aws", ".env"])
    def test_default_deny_blocked(self, workdir, path) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": path})
        assert error_code(msg) == "DENIED_PATH"

    def test_default_deny_disabled(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        call_success(srv, "create_directory", {"workdir": "test", "path": ".ssh"})
        assert (workdir.container_path / ".ssh").is_dir()

    def test_extra_deny_still_applies(self, workdir) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("internal/**",),
        )
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": "internal"})
        assert error_code(msg) == "DENIED_PATH"

    def test_reserved_namespace_blocked(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        msg = call_error(srv, "create_directory", {"workdir": "test", "path": ".serverfs-tmp-dir"})
        assert error_code(msg) == "RESERVED_PATH"


class TestDeleteDirectorySuccess:
    def test_deletes_empty_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "obsolete")
        revision = revision_of(srv, "obsolete")
        data = call_success(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "obsolete", "expected_revision": revision},
        )
        assert data["deleted"] is True
        assert data["revision_deleted"] == revision
        assert not (workdir.container_path / "obsolete").exists()

    def test_delete_children_then_the_directory(self, workdir) -> None:
        """The documented cleanup workflow: list, delete files, delete dir."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / "a.txt").write_text("a")
        (workdir.container_path / "dir" / "b.txt").write_text("b")
        rooted = {"workdir": "test"}
        listing = call_success(srv, "list_directory", {**rooted, "path": "dir"})
        for entry in listing["entries"]:
            path = entry["path"]
            call_success(
                srv,
                "delete_file",
                {**rooted, "path": path, "expected_revision": revision_of(srv, path)},
            )
        call_success(
            srv,
            "delete_directory",
            {
                **rooted,
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert not (workdir.container_path / "dir").exists()

    def test_revision_changes_when_children_are_added(self, workdir) -> None:
        """A directory that gained an entry is no longer the one the agent
        inspected."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        revision = revision_of(srv, "dir")
        (workdir.container_path / "dir" / "child.txt").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "dir", "expected_revision": revision},
        )
        assert error_code(msg) == "REVISION_CONFLICT"


class TestDeleteDirectoryRules:
    def test_non_empty_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / "child.txt").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert error_code(msg) == "DIRECTORY_NOT_EMPTY"
        assert (workdir.container_path / "dir" / "child.txt").exists()

    def test_hidden_child_still_counts(self, workdir) -> None:
        """Emptiness is physical, not "what this agent can see"."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / ".hidden").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert error_code(msg) == "DIRECTORY_NOT_EMPTY"
        assert (workdir.container_path / "dir" / ".hidden").exists()

    def test_denied_child_still_counts(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, extra_deny_globs=("secret*",))
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / "secret.txt").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert error_code(msg) == "DIRECTORY_NOT_EMPTY"

    def test_reserved_temp_child_still_counts(self, workdir) -> None:
        """A leftover temp artifact blocks deletion instead of being
        silently cleaned up by a recursive delete that does not exist."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / ".serverfs-tmp-leftover").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert error_code(msg) == "DIRECTORY_NOT_EMPTY"
        assert (workdir.container_path / "dir" / ".serverfs-tmp-leftover").exists()

    def test_no_error_message_leaks_the_child_name(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / "very-secret-name.dat").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": revision_of(srv, "dir"),
            },
        )
        assert "very-secret-name" not in msg

    def test_stale_revision(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        revision = revision_of(srv, "dir")
        (workdir.container_path / "dir" / "child.txt").write_text("x")
        (workdir.container_path / "dir" / "child.txt").unlink()
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "dir", "expected_revision": revision},
        )
        assert error_code(msg) == "REVISION_CONFLICT"
        assert (workdir.container_path / "dir").is_dir()

    def test_revision_is_required(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "dir")
        msg = call_error(srv, "delete_directory", {"workdir": "test", "path": "dir"})
        assert "expected_revision" in msg

    def test_symlink_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        os.symlink("real", workdir.container_path / "lnk")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "lnk", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert (workdir.container_path / "lnk").is_symlink()
        assert (workdir.container_path / "real").is_dir()

    def test_regular_file_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "afile").write_text("x")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "afile", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "NOT_A_DIRECTORY"
        assert (workdir.container_path / "afile").exists()

    def test_missing_directory(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "ghost", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "PATH_NOT_FOUND"

    def test_workdir_root(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"
        assert workdir.container_path.is_dir()

    def test_read_only_workdir(self, workdir) -> None:
        srv = make_server(workdir)
        os.mkdir(workdir.container_path / "dir")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": "dir", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"
        assert (workdir.container_path / "dir").is_dir()

    def test_hidden_path_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / ".cache")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": ".cache", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "HIDDEN_PATH_NOT_ALLOWED"

    def test_reserved_path_blocked(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        os.mkdir(workdir.container_path / ".serverfs-tmp-dir")
        msg = call_error(
            srv,
            "delete_directory",
            {"workdir": "test", "path": ".serverfs-tmp-dir", "expected_revision": "v1:x"},
        )
        assert error_code(msg) == "RESERVED_PATH"
        assert (workdir.container_path / ".serverfs-tmp-dir").is_dir()
