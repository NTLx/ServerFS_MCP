"""Path security tests: the most critical suite in the project."""

from __future__ import annotations

import os
import socket

import pytest

from serverfs_mcp.paths import (
    AbsolutePathError,
    DeniedPathError,
    HiddenPathNotAllowedError,
    NulPathError,
    PathOutsideWorkdirError,
    PathSecurityError,
    SymlinkNotAllowedError,
    resolve_workdir_path,
)


@pytest.fixture()
def workdir(registry, settings):
    return registry.get("test")


def resolve(wd, path: str, allow_hidden: bool = False):
    return resolve_workdir_path(wd, path, allow_hidden=allow_hidden)


class TestNormalPaths:
    def test_root(self, workdir) -> None:
        r = resolve(workdir, "")
        assert r.container_path == workdir.container_path

    def test_normal_relative(self, workdir, tmp_path) -> None:
        f = workdir.container_path / "a.txt"
        f.write_text("x")
        r = resolve(workdir, "a.txt")
        assert r.container_path == f
        assert r.rel_path == "a.txt"

    def test_dotted_normalized(self, workdir) -> None:
        f = workdir.container_path / "bar.txt"
        f.write_text("x")
        r = resolve(workdir, "foo/../bar.txt")
        assert r.rel_path == "bar.txt"

    def test_trailing_slash(self, workdir) -> None:
        (workdir.container_path / "sub").mkdir()
        r = resolve(workdir, "sub/")
        # trailing slash collapses to the same normalized path
        assert r.rel_path == "sub"


class TestTraversal:
    @pytest.mark.parametrize(
        "path",
        ["../sibling", "../../etc/passwd", "foo/../../../etc/passwd", ".."],
    )
    def test_traversal_rejected(self, workdir, path) -> None:
        with pytest.raises(PathOutsideWorkdirError):
            resolve(workdir, path)

    @pytest.mark.parametrize("path", ["/etc/passwd", "/root/foo", "//etc/passwd"])
    def test_absolute_rejected(self, workdir, path) -> None:
        with pytest.raises(AbsolutePathError):
            resolve(workdir, path)

    def test_windows_style_rejected(self, workdir) -> None:
        # backslash is a literal filename character, never a separator; the
        # whole string stays one component named '..\..\etc' -> still rejected
        with pytest.raises(PathSecurityError):
            resolve(workdir, "..\\..\\etc")

    def test_nul_rejected(self, workdir) -> None:
        with pytest.raises(NulPathError):
            resolve(workdir, "foo\x00bar")

    def test_traversal_normalized_to_inside_ok(self, workdir) -> None:
        (workdir.container_path / "sub").mkdir()
        r = resolve(workdir, "sub/./../sub")
        assert r.rel_path == "sub"


class TestSymlinks:
    def _mklink(self, workdir, name: str, target: str) -> None:
        os.symlink(target, workdir.container_path / name)

    def test_symlink_to_etc_rejected(self, workdir) -> None:
        self._mklink(workdir, "etclink", "/etc")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "etclink")

    def test_path_through_symlink_dir_rejected(self, workdir) -> None:
        self._mklink(workdir, "etclink", "/etc")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "etclink/passwd")

    def test_symlink_inside_workdir_rejected(self, workdir) -> None:
        (workdir.container_path / "real.txt").write_text("x")
        self._mklink(workdir, "selflink", "real.txt")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "selflink")

    def test_symlink_dir_inside_workdir_rejected(self, workdir) -> None:
        (workdir.container_path / "realdir").mkdir()
        self._mklink(workdir, "dirlink", "realdir")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "dirlink")

    def test_symlink_to_another_workdir_rejected(self, workdir, workdir_root) -> None:
        (workdir_root / "02" / "a.txt").write_text("secret")
        self._mklink(workdir, "crosslink", "../02")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "crosslink/a.txt")

    def test_broken_symlink_rejected(self, workdir) -> None:
        self._mklink(workdir, "broken", "does-not-exist")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "broken")

    def test_nested_symlink_component_rejected(self, workdir) -> None:
        (workdir.container_path / "real").mkdir()
        self._mklink(workdir, "lnk", "real")
        (workdir.container_path / "real" / "file.txt").write_text("x")
        with pytest.raises(SymlinkNotAllowedError):
            resolve(workdir, "lnk/file.txt")


class TestHidden:
    def test_hidden_file_rejected(self, workdir) -> None:
        (workdir.container_path / ".env").write_text("SECRET=1")
        with pytest.raises(HiddenPathNotAllowedError):
            resolve(workdir, ".env")

    def test_hidden_dir_rejected(self, workdir) -> None:
        (workdir.container_path / ".config").mkdir()
        (workdir.container_path / ".config" / "cfg.ini").write_text("x")
        with pytest.raises(HiddenPathNotAllowedError):
            resolve(workdir, ".config/cfg.ini")

    def test_hidden_dir_component_blocks_child(self, workdir) -> None:
        (workdir.container_path / ".h").mkdir()
        (workdir.container_path / ".h" / "a.txt").write_text("x")
        with pytest.raises(HiddenPathNotAllowedError):
            resolve(workdir, ".h/a.txt")

    def test_allow_hidden_true_permits_dotfile(self, workdir) -> None:
        (workdir.container_path / ".notes").write_text("x")
        r = resolve(workdir, ".notes", allow_hidden=True)
        assert r.rel_path == ".notes"

    def test_dot_and_dotdot_pass_semantics(self, workdir) -> None:
        (workdir.container_path / "a.txt").write_text("x")
        r = resolve(workdir, "./a.txt")
        assert r.rel_path == "a.txt"


class TestDenyRules:
    @pytest.mark.parametrize(
        "name",
        [".env", ".env.production", "server.pem", "private.key", "id_rsa", "id_ed25519"],
    )
    def test_denied_basename(self, workdir, name) -> None:
        (workdir.container_path / name).write_text("x")
        with pytest.raises(DeniedPathError):
            resolve(workdir, name, allow_hidden=True)

    @pytest.mark.parametrize("dirname", [".ssh", ".aws", ".gnupg", ".kube"])
    def test_denied_dir(self, workdir, dirname) -> None:
        d = workdir.container_path / dirname
        d.mkdir()
        (d / "config").write_text("x")
        with pytest.raises(DeniedPathError):
            resolve(workdir, f"{dirname}/config", allow_hidden=True)

    def test_deny_survives_normalization(self, workdir) -> None:
        (workdir.container_path / ".env").write_text("x")
        with pytest.raises(DeniedPathError):
            resolve(workdir, "sub/../.env", allow_hidden=True)


class TestSpecialFiles:
    def test_fifo_rejected_before_open(self, workdir) -> None:
        fifo = workdir.container_path / "apipe"
        os.mkfifo(fifo)
        from serverfs_mcp.filesystem import check_supported_file_type
        from serverfs_mcp.paths import UnsupportedFileTypeError

        r = resolve(workdir, "apipe")
        with pytest.raises(UnsupportedFileTypeError):
            check_supported_file_type(r)

    def test_unix_socket_rejected_before_open(self, workdir) -> None:
        sock_path = workdir.container_path / "asock"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        from serverfs_mcp.filesystem import check_supported_file_type
        from serverfs_mcp.paths import UnsupportedFileTypeError

        r = resolve(workdir, "asock")
        with pytest.raises(UnsupportedFileTypeError):
            check_supported_file_type(r)
