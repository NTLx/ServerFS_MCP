"""stat_file behaviour."""

from __future__ import annotations

import pytest

from serverfs_mcp.filesystem import stat_file
from serverfs_mcp.paths import resolve_workdir_path


def resolve(workdir, path=""):
    return resolve_workdir_path(workdir, path, allow_hidden=False)


class TestStatFile:
    def test_file_stat(self, workdir) -> None:
        root = workdir.container_path
        (root / "app.py").write_text("print('x')\n")
        result = stat_file(resolve(workdir, "app.py"))
        assert result.type == "file"
        assert result.size == len("print('x')\n")
        assert result.workdir == "test"
        assert result.path == "app.py"
        assert result.modified_at is not None and result.modified_at.endswith("Z")
        assert result.mime_type == "text/x-python"

    def test_yaml_mime(self, workdir) -> None:
        root = workdir.container_path
        (root / "dc.yml").write_text("services: {}\n")
        result = stat_file(resolve(workdir, "dc.yml"))
        assert result.mime_type is not None and "yaml" in result.mime_type

    def test_directory_stat(self, workdir) -> None:
        root = workdir.container_path
        (root / "sub").mkdir()
        result = stat_file(resolve(workdir, "sub"))
        assert result.type == "directory"
        assert result.size is None
        assert result.mime_type is None

    def test_stat_final_symlink_reported_as_symlink(self, workdir) -> None:
        """§25: lstat semantics on the final component; target never revealed."""
        import os

        root = workdir.container_path
        (root / "real.txt").write_text("x")
        os.symlink("real.txt", root / "latest")
        result = stat_file(resolve(workdir, "latest"))
        assert result.type == "symlink"
        assert result.size is None
        assert result.mime_type is None

    def test_missing_file(self, workdir) -> None:
        with pytest.raises(FileNotFoundError):
            stat_file(resolve(workdir, "nope.txt"))

    def test_no_uid_gid_inode_leak(self, workdir) -> None:
        """The result model must carry no host-identifying stat fields."""
        root = workdir.container_path
        (root / "f.txt").write_text("x")
        result = stat_file(resolve(workdir, "f.txt"))
        dump = result.model_dump()
        for forbidden in ("uid", "gid", "inode", "device", "st_ino", "st_dev"):
            assert forbidden not in dump
