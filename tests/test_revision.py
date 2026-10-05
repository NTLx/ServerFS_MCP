"""§104: revision semantics — stability, opacity, change detection.

A revision is the token an agent passes back as expected_revision, so it
must be stable for an unchanged object, change for both content and
metadata changes, be identical across pages of one read, and never carry
raw inode/UID/GID values.
"""

from __future__ import annotations

import os
import re
import time

import pytest

from helpers import call_error, call_success, error_code, make_server, read_write
from platform_contract import WINDOWS, linux_only, settle_file_time

REVISION_RE = re.compile(r"^v1:[0-9a-f]{16}$")


class TestRevisionStability:
    def test_create_revision_matches_stat_and_read(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        created = call_success(
            srv, "create_text_file", {"workdir": "test", "path": "a.txt", "content": "one\n"}
        )
        stat = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        read = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert created["revision"] == stat["revision"] == read["revision"]

    def test_edit_revision_matches_stat_and_read(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "a.txt").write_text("one\n")
        before = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        edited = call_success(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": before["revision"],
                "edits": [{"old_text": "one", "new_text": "two"}],
            },
        )
        stat = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        read = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert edited["revision"] == stat["revision"] == read["revision"]
        assert edited["revision_before"] == before["revision"]
        assert edited["revision"] != before["revision"]

    def test_returned_revision_is_usable_for_the_next_edit(self, workdir) -> None:
        """The token from an edit must be accepted by the following edit —
        a token that is stale the moment it is issued would be useless."""
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "a.txt").write_text("one\n")
        rev = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})["revision"]
        for old, new in (("one", "two"), ("two", "three"), ("three", "four")):
            edited = call_success(
                srv,
                "edit_text_file",
                {
                    "workdir": "test",
                    "path": "a.txt",
                    "expected_revision": rev,
                    "edits": [{"old_text": old, "new_text": new}],
                },
            )
            rev = edited["revision"]
        assert (workdir.container_path / "a.txt").read_text() == "four\n"

    def test_directory_create_revision_matches_stat(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        created = call_success(srv, "create_directory", {"workdir": "test", "path": "sub"})
        stat = call_success(srv, "stat_file", {"workdir": "test", "path": "sub"})
        assert created["revision"] == stat["revision"]

    def test_repeated_stat_is_stable(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_text("stable\n")
        first = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        time.sleep(0.01)
        second = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert first["revision"] == second["revision"]

    def test_reading_does_not_change_revision(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_text("read me\n")
        before = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        after = call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert before["revision"] == after["revision"]


class TestRevisionChanges:
    def test_content_change_changes_revision(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_text("one\n")
        before = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        settle_file_time()
        target.write_text("two\n")
        after = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert before["revision"] != after["revision"]

    @linux_only("POSIX permission bits")
    def test_metadata_change_changes_revision(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_text("same content\n")
        before = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        os.chmod(target, 0o600)
        after = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert before["revision"] != after["revision"]

    def test_directory_revision_changes_with_entries(self, workdir) -> None:
        srv = make_server(workdir)
        os.mkdir(workdir.container_path / "sub")
        before = call_success(srv, "stat_file", {"workdir": "test", "path": "sub"})
        settle_file_time()
        (workdir.container_path / "sub" / "child.txt").write_text("x")
        after = call_success(srv, "stat_file", {"workdir": "test", "path": "sub"})
        assert before["revision"] != after["revision"]

    @linux_only("POSIX symlink semantics; reparse handling is covered on Windows")
    def test_symlink_stat_has_revision(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "real.txt").write_text("x")
        os.symlink("real.txt", workdir.container_path / "lnk")
        data = call_success(srv, "stat_file", {"workdir": "test", "path": "lnk"})
        assert data["type"] == "symlink"
        assert REVISION_RE.match(data["revision"])


class TestPagedReads:
    def test_pages_share_one_revision(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "big.txt").write_text("line\n" * 400)
        first = call_success(
            srv, "read_text_file", {"workdir": "test", "path": "big.txt", "max_lines": 10}
        )
        second = call_success(
            srv,
            "read_text_file",
            {"workdir": "test", "path": "big.txt", "start_line": 11, "max_lines": 10},
        )
        assert first["has_more"] is True
        assert first["revision"] == second["revision"]

    def test_modification_between_pages_shows_new_revision(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "big.txt"
        target.write_text("line\n" * 400)
        first = call_success(
            srv, "read_text_file", {"workdir": "test", "path": "big.txt", "max_lines": 10}
        )
        target.write_text("line\n" * 400 + "appended\n")
        second = call_success(
            srv,
            "read_text_file",
            {"workdir": "test", "path": "big.txt", "start_line": 11, "max_lines": 10},
        )
        assert first["revision"] != second["revision"]

    def test_resource_read_still_works(self, workdir) -> None:
        """The resource template shares the read implementation and keeps
        being all-or-nothing."""
        import asyncio

        srv = make_server(workdir)
        (workdir.container_path / "small.txt").write_bytes(b"hello\n")

        async def _read():
            result = await srv.read_resource("serverfs://test/small.txt")
            return result[0].content

        assert asyncio.run(_read()) == "hello\n"


class TestRevisionOpacity:
    def test_token_shape(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_text("x\n")
        data = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        assert REVISION_RE.match(data["revision"]), data["revision"]

    @linux_only("POSIX stat device identity")
    def test_raw_identity_values_are_not_exposed(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_text("x\n")
        st = os.lstat(target)
        revisions = {
            call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"],
            call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})["revision"],
        }
        for revision in revisions:
            assert str(st.st_ino) not in revision
            assert str(st.st_uid) not in revision
            assert str(st.st_gid) not in revision
            assert str(st.st_dev) not in revision

    def test_identical_content_in_different_files_differs(self, workdir) -> None:
        """Two files with equal bytes must not share a revision: the token
        identifies the object, not just its content."""
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_text("same\n")
        (workdir.container_path / "b.txt").write_text("same\n")
        a = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})
        b = call_success(srv, "stat_file", {"workdir": "test", "path": "b.txt"})
        assert a["revision"] != b["revision"]


class TestFileChangedDuringRead:
    """A read must never return content whose revision does not describe it."""

    @linux_only("simulated through the Linux FD revision path (mutations/fdio)")
    def test_read_detects_a_change_mid_read(self, workdir, monkeypatch) -> None:
        """Simulated by making the second identity check disagree with the
        first — the real trigger is an external write during the read."""
        from serverfs_mcp import mutations

        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_bytes(b"content\n")
        real = mutations.compute_revision
        calls = {"n": 0}

        def flaky(st):
            calls["n"] += 1
            return "v1:" + "0" * 16 if calls["n"] == 2 else real(st)

        # The revision computation now lives behind the backend session; the
        # product layer (tools) no longer imports it. Patch it where the
        # Linux session resolves it at call time (mutations module).
        monkeypatch.setattr(mutations, "compute_revision", flaky)
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})
        assert error_code(msg) == "FILE_CHANGED_DURING_READ"
        assert calls["n"] == 2

    def test_unchanged_file_is_not_flagged(self, workdir) -> None:
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_bytes(b"content\n")
        assert (
            call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})["content"]
            == "content\n"
        )


class TestRevisionForReadOnlyWorkdir:
    def test_read_reports_revision_without_mutation_rights(self, workdir) -> None:
        """Revisions are a read feature too: a read-only workdir still
        reports them (they are what a later edit would need)."""
        srv = make_server(workdir)
        (workdir.container_path / "a.txt").write_text("x\n")
        assert call_success(srv, "read_text_file", {"workdir": "test", "path": "a.txt"})["revision"]
        assert read_write(workdir).read_only is False


BLIND_WINDOW_TRIALS = 200


@pytest.mark.skipif(not WINDOWS, reason="the Windows clock-step boundary has no POSIX equivalent")
class TestWindowsAcceptedRevisionBoundary:
    """What contract decision B promises, and what it deliberately does not (dev_plan_v0.11 §15).

    The Windows revision is a metadata-derived optimistic-concurrency token. An observable metadata
    change is therefore detected; a same-object, same-size in-place rewrite that completes inside
    one filesystem timestamp tick is the single accepted blind window. The second case is measured
    on demand rather than carried as a permanent expected-failure, because the release suite must
    not encode a guarantee the contract does not make.
    """

    def test_an_explicit_timestamp_move_changes_the_revision(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_text("same size\n")
        before = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        stamp = time.time() + 5.0
        os.utime(target, (stamp, stamp))
        after = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        assert before != after

    def test_a_size_change_and_a_replacement_change_the_revision(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_text("one\n")
        first = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        target.write_text("much longer content\n")
        second = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        assert first != second
        staged = workdir.container_path / "staged.txt"
        staged.write_text("much longer content\n")
        os.replace(staged, target)
        third = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        assert second != third, "object replacement must be visible to the token"

    @pytest.mark.skipif(
        not os.environ.get("SERVERFS_MEASURE_BLIND_WINDOW"),
        reason="measurement probe; run it when the revision contract is revisited",
    )
    def test_same_tick_same_size_rewrite_is_measured(self, workdir) -> None:
        srv = make_server(workdir)
        target = workdir.container_path / "a.txt"
        target.write_bytes(b"x" * 32 + b"\n")
        unchanged = 0
        changed = 0
        for _ in range(BLIND_WINDOW_TRIALS):
            before = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})[
                "revision"
            ]
            target.write_bytes(b"y" * 32 + b"\n")
            after = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
            if before == after:
                unchanged += 1
            else:
                changed += 1
        print(
            f"blind-window probe: {unchanged} unchanged, {changed} detected "
            f"over {BLIND_WINDOW_TRIALS} tight same-size rewrites"
        )
        assert unchanged + changed == BLIND_WINDOW_TRIALS
