"""list_directory behaviour."""

from __future__ import annotations

import os

import pytest

from serverfs_mcp.filesystem import list_directory
from serverfs_mcp.paths import resolve_workdir_path


def resolve(workdir, path=""):
    return resolve_workdir_path(workdir, path, allow_hidden=False)


class TestBasics:
    def test_empty_directory(self, workdir) -> None:
        entries, has_more = list_directory(resolve(workdir), offset=0, limit=100)
        assert entries == []
        assert has_more is False

    def test_files_and_directories(self, workdir) -> None:
        root = workdir.container_path
        (root / "b.txt").write_text("x")
        (root / "a.txt").write_text("y")
        (root / "sub").mkdir()
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        by_name = {e.name: e for e in entries}
        assert set(by_name) == {"a.txt", "b.txt", "sub"}
        assert by_name["a.txt"].type == "file"
        assert by_name["a.txt"].size == 1
        assert by_name["sub"].type == "directory"
        assert by_name["sub"].size is None
        assert by_name["a.txt"].path == "a.txt"
        assert by_name["sub"].path == "sub"

    def test_nested_path_prefix(self, workdir) -> None:
        root = workdir.container_path
        (root / "sub").mkdir()
        (root / "sub" / "inner.txt").write_text("x")
        entries, _ = list_directory(resolve(workdir, "sub"), offset=0, limit=100)
        assert entries[0].path == "sub/inner.txt"

    def test_stable_alphabetical_sorting(self, workdir) -> None:
        root = workdir.container_path
        for name in ["z.txt", "a.txt", "m.txt", "b.txt", "0.txt"]:
            (root / name).write_text("x")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        names = [e.name for e in entries]
        assert names == sorted(names)

    def test_modified_at_rfc3339_utc(self, workdir) -> None:
        root = workdir.container_path
        (root / "t.txt").write_text("x")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        ts = entries[0].modified_at
        assert ts is not None and ts.endswith("Z") and "T" in ts
        # e.g. 2026-09-17T01:20:30Z
        assert len(ts) == 20


class TestEntryTypes:
    def test_fifo_reported_as_other(self, workdir) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        by_name = {e.name: e for e in entries}
        assert by_name["apipe"].type == "other"

    def test_socket_reported_as_other(self, workdir) -> None:
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(workdir.container_path / "asock"))
        s.close()
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        by_name = {e.name: e for e in entries}
        assert by_name["asock"].type == "other"

    def test_entry_type_matrix(self, workdir) -> None:
        root = workdir.container_path
        (root / "f.txt").write_text("x")
        (root / "d").mkdir()
        os.symlink("f.txt", root / "l")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        by_name = {e.name: e for e in entries}
        assert by_name["f.txt"].type == "file"
        assert by_name["d"].type == "directory"
        assert by_name["l"].type == "symlink"


class TestSymlinks:
    def test_symlink_visible_typed(self, workdir) -> None:
        root = workdir.container_path
        (root / "real.txt").write_text("x")
        os.symlink("real.txt", root / "latest")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        by_name = {e.name: e for e in entries}
        assert by_name["latest"].type == "symlink"
        # target must not be exposed
        assert by_name["latest"].size is None


class TestPagination:
    @pytest.fixture()
    def many(self, workdir) -> None:
        root = workdir.container_path
        for i in range(10):
            (root / f"f{i:02d}.txt").write_text("x")

    def test_limit(self, registry, workdir, many) -> None:
        entries, has_more = list_directory(resolve(workdir), offset=0, limit=4)
        assert len(entries) == 4
        assert has_more is True

    def test_offset(self, workdir, many) -> None:
        entries, has_more = list_directory(resolve(workdir), offset=8, limit=4)
        assert [e.name for e in entries] == ["f08.txt", "f09.txt"]
        assert has_more is False

    def test_offset_beyond(self, workdir, many) -> None:
        entries, has_more = list_directory(resolve(workdir), offset=50, limit=4)
        assert entries == []
        assert has_more is False


class TestFiltering:
    def test_hidden_filtered(self, workdir) -> None:
        root = workdir.container_path
        (root / ".hidden").write_text("x")
        (root / "visible.txt").write_text("x")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        assert [e.name for e in entries] == ["visible.txt"]

    def test_denied_files_filtered(self, workdir) -> None:
        root = workdir.container_path
        (root / ".env").write_text("SECRET=1")
        (root / "ok.txt").write_text("x")
        (root / "cert.pem").write_text("x")
        entries, _ = list_directory(resolve(workdir), offset=0, limit=100)
        # .env filtered as hidden; cert.pem filtered by deny rule
        assert [e.name for e in entries] == ["ok.txt"]

    def test_not_a_directory_error(self, workdir) -> None:
        root = workdir.container_path
        (root / "plain.txt").write_text("x")
        with pytest.raises(NotADirectoryError):
            list_directory(resolve(workdir, "plain.txt"), offset=0, limit=10)

    def test_missing_dir_raises(self, workdir) -> None:
        with pytest.raises(FileNotFoundError):
            list_directory(resolve(workdir, "no-such-dir"), offset=0, limit=10)
