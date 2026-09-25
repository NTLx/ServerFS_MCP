"""FD-based traversal security tests (fdio primitives).

These exercise the O_NOFOLLOW / dir_fd walk directly — the layer that
makes symlink enforcement and object identity atomic (no lstat→open race).
"""

from __future__ import annotations

import errno
import os
import socket
import stat as stat_module
import threading

import pytest

from serverfs_mcp.fdio import (
    open_directory_fd,
    open_file_fd,
    open_root,
    stat_final,
    walk_parent_dirs,
)
from serverfs_mcp.paths import (
    ResourceExhaustedError,
    SymlinkNotAllowedError,
    UnsupportedFileTypeError,
)


@pytest.fixture()
def root_fd(workdir):
    fd = os.open(str(workdir.container_path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def _mklink(workdir, name: str, target: str) -> None:
    os.symlink(target, workdir.container_path / name)


def test_emfile_is_resource_exhausted(monkeypatch) -> None:
    def fail_open(*args, **kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(ResourceExhaustedError):
        open_root("/unused")


class TestParentSymlinkWalk:
    """A symlink ANYWHERE on the parent chain must break the walk."""

    def test_symlink_to_etc(self, workdir, root_fd) -> None:
        _mklink(workdir, "etclink", "/etc")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("etclink",)):
                pass

    def test_path_through_symlink_dir(self, workdir, root_fd) -> None:
        _mklink(workdir, "etclink", "/etc")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("etclink", "sub")):
                pass

    def test_symlink_inside_workdir(self, workdir, root_fd) -> None:
        (workdir.container_path / "real.txt").write_text("x")
        _mklink(workdir, "selflink", "real.txt")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("selflink", "x")):
                pass

    def test_symlink_dir_inside_workdir(self, workdir, root_fd) -> None:
        (workdir.container_path / "realdir").mkdir()
        _mklink(workdir, "dirlink", "realdir")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("dirlink", "f")):
                pass

    def test_symlink_to_another_workdir(self, workdir, workdir_root, root_fd) -> None:
        (workdir_root / "02" / "a.txt").write_text("secret")
        _mklink(workdir, "crosslink", "../02")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("crosslink", "a.txt")):
                pass

    def test_broken_symlink(self, workdir, root_fd) -> None:
        _mklink(workdir, "broken", "does-not-exist")
        # opening a broken symlink dir with O_NOFOLLOW|O_DIRECTORY: ENOENT
        with pytest.raises((SymlinkNotAllowedError, FileNotFoundError)):
            with walk_parent_dirs(root_fd, ("broken", "f")):
                pass

    def test_nested_symlink_component(self, workdir, root_fd) -> None:
        (workdir.container_path / "real").mkdir()
        (workdir.container_path / "real" / "f.txt").write_text("x")
        _mklink(workdir, "lnk", "real")
        with pytest.raises(SymlinkNotAllowedError):
            with walk_parent_dirs(root_fd, ("lnk", "f.txt")):
                pass


class TestOpenFileFd:
    def test_final_symlink_rejected(self, workdir, root_fd) -> None:
        (workdir.container_path / "real.txt").write_text("x")
        _mklink(workdir, "lnk", "real.txt")
        with pytest.raises(SymlinkNotAllowedError):
            with open_file_fd(root_fd, ("lnk",)):
                pass

    def test_directory_rejected_as_not_a_file(self, workdir, root_fd) -> None:
        (workdir.container_path / "adir").mkdir()
        with pytest.raises(NotADirectoryError):
            with open_file_fd(root_fd, ("adir",)):
                pass

    def test_fifo_rejected_before_read(self, workdir, root_fd) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        with pytest.raises((UnsupportedFileTypeError, OSError)):
            with open_file_fd(root_fd, ("apipe",)):
                pass

    def test_unix_socket_rejected(self, workdir, root_fd) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(workdir.container_path / "asock"))
        s.close()
        with pytest.raises((UnsupportedFileTypeError, OSError)):
            with open_file_fd(root_fd, ("asock",)):
                pass

    def test_regular_file_opens(self, workdir, root_fd) -> None:
        (workdir.container_path / "ok.txt").write_bytes(b"hello\n")
        with open_file_fd(root_fd, ("ok.txt",)) as fd:
            data = os.read(fd, 16)
            assert data == b"hello\n"

    def test_root_path_rejected_as_file(self, root_fd) -> None:
        with pytest.raises(NotADirectoryError):
            with open_file_fd(root_fd, ()):
                pass


class TestStatFinal:
    def test_final_symlink_reported_not_followed(self, workdir, root_fd) -> None:
        (workdir.container_path / "real.txt").write_text("x")
        _mklink(workdir, "lnk", "real.txt")
        st = stat_final(root_fd, ("lnk",))
        assert stat_module.S_ISLNK(st.st_mode)

    def test_symlink_parent_rejected(self, workdir, root_fd) -> None:
        _mklink(workdir, "etclink", "/etc")
        with pytest.raises(SymlinkNotAllowedError):
            stat_final(root_fd, ("etclink", "passwd"))

    def test_fifo_reported(self, workdir, root_fd) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        st = stat_final(root_fd, ("apipe",))
        assert stat_module.S_ISFIFO(st.st_mode)


class TestOpenDirectoryFd:
    def test_opens_subdirectory(self, workdir, root_fd) -> None:
        (workdir.container_path / "sub").mkdir()
        with open_directory_fd(root_fd, ("sub",)) as fd:
            names = [e.name for e in os.scandir(fd)]
            assert names == []

    def test_root_dup(self, root_fd) -> None:
        with open_directory_fd(root_fd, ()) as fd:
            assert fd != root_fd  # independent descriptor
            assert os.fstat(fd) == os.fstat(root_fd)

    def test_file_rejected_as_directory(self, workdir, root_fd) -> None:
        (workdir.container_path / "f.txt").write_text("x")
        with pytest.raises(NotADirectoryError):
            with open_directory_fd(root_fd, ("f.txt",)):
                pass


class TestToctouRace:
    """Concurrent replace file↔symlink during validation/open.

    The security property: with FD-based traversal, reads either succeed
    on a regular file or fail with a coded error — never read a symlink
    target outside the workdir. O_NOFOLLOW makes this structural, so the
    race here is a regression probe, not the primary defense.
    """

    def test_file_symlink_swap_cannot_escape(self, workdir, root_fd, tmp_path) -> None:
        target_outside = tmp_path / "outside.txt"
        target_outside.write_text("OUTSIDE SECRET\n")
        victim = workdir.container_path / "victim.txt"
        (workdir.container_path / "victim.txt").write_text("inside\n")

        stop = threading.Event()
        swap_errors = 0

        def swapper() -> None:
            nonlocal swap_errors
            while not stop.is_set():
                try:
                    (workdir.container_path / "victim.txt").unlink()
                    os.symlink(str(target_outside), victim)
                    (workdir.container_path / "victim.txt").unlink()
                    (workdir.container_path / "victim.txt").write_text("inside\n")
                except OSError:
                    swap_errors += 1

        t = threading.Thread(target=swapper)
        t.start()
        leaked = False
        try:
            for _ in range(200):
                try:
                    with open_file_fd(root_fd, ("victim.txt",)) as fd:
                        st = os.fstat(fd)
                        assert stat_module.S_ISREG(st.st_mode)  # invariant
                        data = os.read(fd, 64)
                    if b"OUTSIDE SECRET" in data:
                        leaked = True
                        break
                except (SymlinkNotAllowedError, FileNotFoundError, OSError):
                    continue
        finally:
            stop.set()
            t.join()
        assert not leaked
