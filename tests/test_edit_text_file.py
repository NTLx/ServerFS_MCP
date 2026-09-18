"""§97: edit_text_file — exact-match, revision-guarded, atomic replacement."""

from __future__ import annotations

import errno
import os
import stat as stat_module

import pytest

from helpers import call_error, call_success, error_code, make_server

BOM = b"\xef\xbb\xbf"


def seed(workdir, name: str, text: str, *, mode: int | None = None) -> None:
    path = workdir.container_path / name
    path.write_bytes(text.encode())
    if mode is not None:
        os.chmod(path, mode)


def revision_of(srv, path: str) -> str:
    return call_success(srv, "read_text_file", {"workdir": "test", "path": path})["revision"]


def stat_revision_of(srv, path: str) -> str:
    """Revision via stat_file: the only channel for non-text targets."""
    return call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]


def edit(srv, path: str, revision: str, edits: list[dict], **extra):
    args = {
        "workdir": "test",
        "path": path,
        "expected_revision": revision,
        "edits": edits,
    }
    args.update(extra)
    return call_success(srv, "edit_text_file", args)


class TestEditSuccess:
    def test_single_exact_edit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "app.yaml", "port: 8080\nhost: localhost\n")
        data = edit(
            srv,
            "app.yaml",
            revision_of(srv, "app.yaml"),
            [{"old_text": "port: 8080", "new_text": "port: 8081"}],
        )
        assert data["edited"] is True
        assert data["edits_applied"] == 1
        assert data["bytes_before"] == len("port: 8080\nhost: localhost\n")
        assert data["bytes_after"] == data["bytes_before"]
        assert (
            workdir.container_path / "app.yaml"
        ).read_bytes() == b"port: 8081\nhost: localhost\n"

    def test_multiple_edits_apply_in_order(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        data = edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [
                {"old_text": "alpha", "new_text": "beta"},
                {"old_text": "beta", "new_text": "gamma"},
            ],
        )
        assert data["edits_applied"] == 2
        assert (workdir.container_path / "a.txt").read_text() == "gamma\n"

    def test_multiple_occurrences_with_expected_count(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "x\nx\nx\n")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "x", "new_text": "y", "expected_count": 3}],
        )
        assert (workdir.container_path / "a.txt").read_text() == "y\ny\ny\n"

    def test_deletion_via_empty_new_text(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "keep\nremove me\n")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "remove me\n", "new_text": ""}],
        )
        assert (workdir.container_path / "a.txt").read_text() == "keep\n"

    def test_fill_empty_file(self, workdir) -> None:
        """The one legal empty old_text: a completely empty file."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "", "new_text": "first content\n"}],
        )
        assert (workdir.container_path / "a.txt").read_text() == "first content\n"

    def test_no_temp_files_remain(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "alpha", "new_text": "beta"}],
        )
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["a.txt"]

    def test_edited_file_still_reads_through_the_read_channel(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "alpha", "new_text": "beta"}],
        )
        assert (
            call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})["content"]
            == "beta\n"
        )


class TestEditTextFidelity:
    def test_chinese_text(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "cn.txt", "配置项：端口 8080\n")
        edit(
            srv,
            "cn.txt",
            revision_of(srv, "cn.txt"),
            [{"old_text": "端口 8080", "new_text": "端口 9090"}],
        )
        assert (workdir.container_path / "cn.txt").read_text() == "配置项：端口 9090\n"

    def test_bom_is_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "bom.txt").write_bytes(BOM + b"hello\n")
        # read hides the BOM from the agent
        read = call_success(srv, "read_text_file", {"workdir": "test", "path": "bom.txt"})
        assert read["content"] == "hello\n"
        edit(
            srv,
            "bom.txt",
            read["revision"],
            [{"old_text": "hello", "new_text": "world"}],
        )
        assert (workdir.container_path / "bom.txt").read_bytes() == BOM + b"world\n"

    def test_absent_bom_is_not_added(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "plain.txt", "hello\n")
        edit(
            srv,
            "plain.txt",
            revision_of(srv, "plain.txt"),
            [{"old_text": "hello", "new_text": "world"}],
        )
        assert (workdir.container_path / "plain.txt").read_bytes() == b"world\n"

    def test_empty_file_with_bom_can_be_filled(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "bom.txt").write_bytes(BOM)
        edit(
            srv,
            "bom.txt",
            revision_of(srv, "bom.txt"),
            [{"old_text": "", "new_text": "content\n"}],
        )
        assert (workdir.container_path / "bom.txt").read_bytes() == BOM + b"content\n"

    def test_crlf_is_preserved_everywhere_else(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        original = b"one\r\ntwo\r\nthree\r\n"
        (workdir.container_path / "crlf.txt").write_bytes(original)
        edit(
            srv,
            "crlf.txt",
            revision_of(srv, "crlf.txt"),
            [{"old_text": "two", "new_text": "TWO"}],
        )
        assert (workdir.container_path / "crlf.txt").read_bytes() == b"one\r\nTWO\r\nthree\r\n"

    def test_missing_final_newline_is_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "a.txt").write_bytes(b"no trailing newline")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "no trailing", "new_text": "still no trailing"}],
        )
        assert (workdir.container_path / "a.txt").read_bytes() == b"still no trailing newline"

    def test_untouched_bytes_are_identical(self, workdir) -> None:
        """A replacement must not rewrite the parts it did not match."""
        srv = make_server(workdir, read_write_access=True)
        original = "前\n\r\n\tspaces  \n"
        seed(workdir, "a.txt", original)
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "spaces", "new_text": "SPACES"}],
        )
        expected = original.replace("spaces", "SPACES")
        assert (workdir.container_path / "a.txt").read_bytes() == expected.encode()


class TestEditConflicts:
    def test_wrong_occurrence_count(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "x\nx\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "x", "new_text": "y"}],
            },
        )
        assert error_code(msg) == "EDIT_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "x\nx\n"

    def test_missing_old_text(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "absent", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "EDIT_CONFLICT"

    def test_empty_old_text_on_non_empty_file(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "not empty\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "", "new_text": "inserted"}],
            },
        )
        assert error_code(msg) == "EDIT_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "not empty\n"

    def test_empty_old_text_cannot_insert_at_every_position(self, workdir) -> None:
        """expected_count cannot smuggle in an insert-everywhere edit."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "abc")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "", "new_text": "X", "expected_count": 4}],
            },
        )
        assert error_code(msg) == "EDIT_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "abc"

    def test_revision_conflict(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "one\n")
        stale = revision_of(srv, "a.txt")
        (workdir.container_path / "a.txt").write_text("changed by someone else\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": stale,
                "edits": [{"old_text": "one", "new_text": "two"}],
            },
        )
        assert error_code(msg) == "REVISION_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "changed by someone else\n"

    def test_unknown_revision_token(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "one\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "one", "new_text": "two"}],
            },
        )
        assert error_code(msg) == "REVISION_CONFLICT"

    def test_edits_are_all_or_nothing(self, workdir) -> None:
        """The first edit succeeds in memory, the second fails: nothing is
        written."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [
                    {"old_text": "alpha", "new_text": "beta"},
                    {"old_text": "not here", "new_text": "x"},
                ],
            },
        )
        assert error_code(msg) == "EDIT_CONFLICT"
        assert (workdir.container_path / "a.txt").read_text() == "alpha\n"

    def test_too_many_edits(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_edits_per_call=2)
        seed(workdir, "a.txt", "a\nb\nc\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [
                    {"old_text": "a", "new_text": "A"},
                    {"old_text": "b", "new_text": "B"},
                    {"old_text": "c", "new_text": "C"},
                ],
            },
        )
        assert error_code(msg) == "TOO_MANY_EDITS"
        assert (workdir.container_path / "a.txt").read_text() == "a\nb\nc\n"

    def test_empty_edits_list_is_rejected(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "a\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [],
            },
        )
        assert "edits" in msg.lower()


class TestEditTargetRequirements:
    def test_missing_file_is_never_created(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "ghost.txt",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "PATH_NOT_FOUND"
        assert not (workdir.container_path / "ghost.txt").exists()

    def test_mistyped_path_does_not_become_a_new_file(self, workdir) -> None:
        """The reason edit is separate from create: a typo must fail."""
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "config")
        seed(workdir, "config/app.yaml", "port: 8080\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "config/apps.yaml",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "8080", "new_text": "8081"}],
            },
        )
        assert error_code(msg) == "PATH_NOT_FOUND"
        assert sorted(p.name for p in (workdir.container_path / "config").iterdir()) == ["app.yaml"]

    def test_directory_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "sub")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "sub",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "NOT_A_FILE"

    def test_symlink_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "real.txt", "content\n")
        os.symlink("real.txt", workdir.container_path / "lnk.txt")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "lnk.txt",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "content", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"
        assert (workdir.container_path / "real.txt").read_text() == "content\n"

    def test_fifo_target(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkfifo(workdir.container_path / "pipe")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "pipe",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "UNSUPPORTED_FILE_TYPE"

    def test_parent_is_a_symlink(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "real")
        seed(workdir, "real/a.txt", "content\n")
        os.symlink("real", workdir.container_path / "link")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "link/a.txt",
                "expected_revision": "v1:0000000000000000",
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "SYMLINK_NOT_ALLOWED"

    def test_binary_file_with_nul(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "bin").write_bytes(b"PNG\x00\x01\x02")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "bin",
                "expected_revision": stat_revision_of(srv, "bin"),
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "BINARY_FILE"

    def test_invalid_utf8(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "bad").write_bytes(b"caf\xe9 invalid utf8\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "bad",
                "expected_revision": stat_revision_of(srv, "bad"),
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "UNSUPPORTED_TEXT_ENCODING"

    def test_file_larger_than_the_write_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=32)
        seed(workdir, "big.txt", "x" * 33)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "big.txt",
                "expected_revision": revision_of(srv, "big.txt"),
                "edits": [{"old_text": "x", "new_text": "y", "expected_count": 33}],
            },
        )
        assert error_code(msg) == "WRITE_TOO_LARGE"
        assert (workdir.container_path / "big.txt").read_text() == "x" * 33

    def test_result_larger_than_the_write_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=32)
        seed(workdir, "a.txt", "small\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "small", "new_text": "y" * 64}],
            },
        )
        assert error_code(msg) == "WRITE_TOO_LARGE"
        assert (workdir.container_path / "a.txt").read_text() == "small\n"

    def test_edits_payload_larger_than_the_write_limit(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=32)
        seed(workdir, "a.txt", "small\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "small", "new_text": "y" * 64}],
            },
        )
        assert error_code(msg) == "WRITE_TOO_LARGE"

    def test_hardlinked_file_is_refused(self, workdir) -> None:
        """Atomic replacement would silently split the hard link."""
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "shared\n")
        os.link(workdir.container_path / "a.txt", workdir.container_path / "b.txt")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "shared", "new_text": "changed"}],
            },
        )
        assert error_code(msg) == "MULTIPLE_HARDLINKS_NOT_SUPPORTED"
        assert (workdir.container_path / "a.txt").read_text() == "shared\n"
        assert (
            os.lstat(workdir.container_path / "b.txt").st_ino
            == os.lstat(workdir.container_path / "a.txt").st_ino
        )

    def test_read_only_workdir(self, workdir) -> None:
        srv = make_server(workdir)
        seed(workdir, "a.txt", "content\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "content", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"
        assert (workdir.container_path / "a.txt").read_text() == "content\n"

    def test_workdir_root(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "a", "new_text": "b"}],
            },
        )
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"


class TestEditPolicy:
    def test_hidden_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, ".notes", "content\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": ".notes",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "content", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "HIDDEN_PATH_NOT_ALLOWED"

    def test_hidden_allowed(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        seed(workdir, ".notes", "content\n")
        edit(
            srv,
            ".notes",
            revision_of(srv, ".notes"),
            [{"old_text": "content", "new_text": "changed"}],
        )
        assert (workdir.container_path / ".notes").read_text() == "changed\n"

    def test_default_deny_blocked(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True, allow_hidden=True)
        seed(workdir, ".env", "A=1\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": ".env",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "A=1", "new_text": "A=2"}],
            },
        )
        assert error_code(msg) == "DENIED_PATH"

    def test_default_deny_disabled_allows_edit(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        seed(workdir, ".env", "A=1\n")
        edit(
            srv,
            ".env",
            revision_of(srv, ".env"),
            [{"old_text": "A=1", "new_text": "A=2"}],
        )
        assert (workdir.container_path / ".env").read_text() == "A=2\n"

    def test_extra_deny_still_applies(self, workdir) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("*.secret",),
        )
        seed(workdir, "a.secret", "content\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.secret",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "content", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "DENIED_PATH"

    def test_reserved_namespace_blocked(self, workdir) -> None:
        srv = make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )
        seed(workdir, ".serverfs-tmp-abc", "content\n")
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": ".serverfs-tmp-abc",
                "expected_revision": "v1:x",
                "edits": [{"old_text": "content", "new_text": "x"}],
            },
        )
        assert error_code(msg) == "RESERVED_PATH"


class TestEditMetadata:
    def test_mode_is_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n", mode=0o640)
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "content", "new_text": "changed"}],
        )
        mode = stat_module.S_IMODE(os.lstat(workdir.container_path / "a.txt").st_mode)
        assert mode == 0o640

    def test_executable_bit_is_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "script.sh", "#!/bin/sh\necho hi\n", mode=0o755)
        edit(
            srv,
            "script.sh",
            revision_of(srv, "script.sh"),
            [{"old_text": "hi", "new_text": "hello"}],
        )
        mode = stat_module.S_IMODE(os.lstat(workdir.container_path / "script.sh").st_mode)
        assert mode == 0o755

    def test_ownership_is_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n")
        before = os.lstat(workdir.container_path / "a.txt")
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "content", "new_text": "changed"}],
        )
        after = os.lstat(workdir.container_path / "a.txt")
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)

    def test_xattrs_are_preserved(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n")
        target = workdir.container_path / "a.txt"
        try:
            os.setxattr(target, "user.serverfs-test", b"keep-me")
        except OSError as exc:
            if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                pytest.skip("filesystem does not support user xattrs")
            raise
        edit(
            srv,
            "a.txt",
            revision_of(srv, "a.txt"),
            [{"old_text": "content", "new_text": "changed"}],
        )
        assert os.getxattr(target, "user.serverfs-test") == b"keep-me"
        assert (target).read_text() == "changed\n"

    def test_mode_preservation_failure_aborts_the_edit(self, workdir, monkeypatch) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n")

        def boom(*args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "fchmod", boom)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "content", "new_text": "changed"}],
            },
        )
        assert error_code(msg) == "METADATA_PRESERVATION_FAILED"
        assert (workdir.container_path / "a.txt").read_text() == "content\n"
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["a.txt"]

    def test_xattr_preservation_failure_aborts_the_edit(self, workdir, monkeypatch) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "content\n")
        target = workdir.container_path / "a.txt"
        try:
            os.setxattr(target, "user.serverfs-test", b"keep-me")
        except OSError as exc:
            if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                pytest.skip("filesystem does not support user xattrs")
            raise

        def boom(*args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "setxattr", boom)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "content", "new_text": "changed"}],
            },
        )
        assert error_code(msg) == "METADATA_PRESERVATION_FAILED"
        assert (workdir.container_path / "a.txt").read_text() == "content\n"
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["a.txt"]

    def test_ownership_preservation_failure_is_reported(self, workdir, monkeypatch) -> None:
        """A file owned by someone else cannot be replaced with an identical
        owner by a non-root process: that must fail loudly, not silently
        change the owner. Exercised at the primitive level because a test
        process cannot create a foreign-owned file without privileges."""
        from serverfs_mcp import mutations

        seed(workdir, "a.txt", "content\n")
        target = workdir.container_path / "a.txt"
        real = os.lstat(target)
        foreign = os.stat_result(
            (
                real.st_mode,
                real.st_ino,
                real.st_dev,
                real.st_nlink,
                real.st_uid + 4242,
                real.st_gid,
                real.st_size,
                real.st_atime,
                real.st_mtime,
                real.st_ctime,
            )
        )
        src_fd = os.open(target, os.O_RDONLY)
        dst_fd = os.open(
            workdir.container_path / ".serverfs-tmp-unit",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            monkeypatch.setattr(os, "fchown", _raise_eperm)
            with pytest.raises(mutations.MetadataPreservationError, match="ownership"):
                mutations._preserve_metadata(src_fd, dst_fd, foreign)
        finally:
            os.close(src_fd)
            os.close(dst_fd)


def _raise_eperm(*args, **kwargs):
    raise OSError(errno.EPERM, "Operation not permitted")


class TestEditAtomicity:
    def test_replace_failure_leaves_the_original_untouched(self, workdir, monkeypatch) -> None:
        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "original content\n")

        def boom(*args, **kwargs):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "replace", boom)
        msg = call_error(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision_of(srv, "a.txt"),
                "edits": [{"old_text": "original", "new_text": "changed"}],
            },
        )
        assert error_code(msg) == "MUTATION_IO_ERROR"
        assert (workdir.container_path / "a.txt").read_text() == "original content\n"
        assert sorted(p.name for p in workdir.container_path.iterdir()) == ["a.txt"]

    def test_writes_never_expose_a_partial_file(self, workdir) -> None:
        """Readers see the complete old or the complete new content: the
        replacement is a rename, never a truncate-then-write."""
        from helpers import call_concurrently, outcomes

        srv = make_server(workdir, read_write_access=True)
        old = "A" * 4000 + "\n"
        new = "B" * 4000 + "\n"
        seed(workdir, "a.txt", old)
        revision = revision_of(srv, "a.txt")
        results = call_concurrently(
            srv,
            [
                (
                    "edit_text_file",
                    {
                        "workdir": "test",
                        "path": "a.txt",
                        "expected_revision": revision,
                        "edits": [{"old_text": "A", "new_text": "B", "expected_count": 4000}],
                    },
                ),
                ("read_text_file", {"workdir": "test", "path": "a.txt"}),
                ("read_text_file", {"workdir": "test", "path": "a.txt"}),
                ("read_text_file", {"workdir": "test", "path": "a.txt"}),
            ],
        )
        assert "unexpected" not in outcomes(results)
        for outcome, payload in results:
            if outcome != "ok":
                continue
            if "content" in payload:
                assert payload["content"] in (old, new), "partial content observed"
        final = (workdir.container_path / "a.txt").read_text()
        assert final in (old, new)

    def test_concurrent_edits_with_the_same_revision(self, workdir) -> None:
        """Exactly one wins; the loser must not clobber the winner."""
        from helpers import call_concurrently, error_codes, outcomes

        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        revision = revision_of(srv, "a.txt")
        results = call_concurrently(
            srv,
            [
                (
                    "edit_text_file",
                    {
                        "workdir": "test",
                        "path": "a.txt",
                        "expected_revision": revision,
                        "edits": [{"old_text": "alpha", "new_text": "bravo"}],
                    },
                ),
                (
                    "edit_text_file",
                    {
                        "workdir": "test",
                        "path": "a.txt",
                        "expected_revision": revision,
                        "edits": [{"old_text": "alpha", "new_text": "charlie"}],
                    },
                ),
            ],
        )
        assert sorted(outcomes(results)) == ["err", "ok"]
        assert error_codes(results) == ["REVISION_CONFLICT"]
        assert (workdir.container_path / "a.txt").read_text() in ("bravo\n", "charlie\n")

    def test_concurrent_edit_and_delete(self, workdir) -> None:
        """Either the edit or the delete wins — never both, and the file
        never ends up in an inconsistent state."""
        from helpers import call_concurrently, error_codes, outcomes

        srv = make_server(workdir, read_write_access=True)
        seed(workdir, "a.txt", "alpha\n")
        revision = revision_of(srv, "a.txt")
        results = call_concurrently(
            srv,
            [
                (
                    "edit_text_file",
                    {
                        "workdir": "test",
                        "path": "a.txt",
                        "expected_revision": revision,
                        "edits": [{"old_text": "alpha", "new_text": "bravo"}],
                    },
                ),
                (
                    "delete_file",
                    {"workdir": "test", "path": "a.txt", "expected_revision": revision},
                ),
            ],
        )
        edit_outcome, delete_outcome = outcomes(results)
        assert sorted([edit_outcome, delete_outcome]) == ["err", "ok"]
        # the loser reports the newer state: gone (PATH_NOT_FOUND) or changed
        assert error_codes(results)[0] in ("REVISION_CONFLICT", "PATH_NOT_FOUND")
        target = workdir.container_path / "a.txt"
        if target.exists():
            # the delete lost, so the edit must have won and be fully applied
            assert edit_outcome == "ok"
            assert target.read_text() == "bravo\n"
        else:
            assert delete_outcome == "ok"
