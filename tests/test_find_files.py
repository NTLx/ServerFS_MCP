"""find_files behaviour."""

from __future__ import annotations

import os

from serverfs_mcp.filesystem import find_files
from serverfs_mcp.paths import resolve_workdir_path


def resolve(workdir, path=""):
    return resolve_workdir_path(workdir, path, allow_hidden=False)


class TestPatterns:
    def test_star_py(self, workdir) -> None:
        root = workdir.container_path
        (root / "a.py").write_text("x")
        (root / "b.txt").write_text("x")
        matches, truncated = find_files(
            resolve(workdir), pattern="*.py", limit=50, max_walk_entries=200_000
        )
        assert matches == ["a.py"]
        assert truncated is False

    def test_compose_glob(self, workdir) -> None:
        root = workdir.container_path
        (root / "PandaWiki").mkdir()
        (root / "PandaWiki" / "docker-compose.yml").write_text("x")
        (root / "infra").mkdir()
        (root / "infra" / "docker-compose.prod.yml").write_text("x")
        (root / "deploy.txt").write_text("x")
        matches, _ = find_files(
            resolve(workdir), pattern="*compose*.yml", limit=50, max_walk_entries=200_000
        )
        assert set(matches) == {
            "PandaWiki/docker-compose.yml",
            "infra/docker-compose.prod.yml",
        }

    def test_nested_directories(self, workdir) -> None:
        root = workdir.container_path
        deep = root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "target.md").write_text("x")
        matches, _ = find_files(
            resolve(workdir), pattern="target.md", limit=50, max_walk_entries=200_000
        )
        assert matches == ["a/b/c/target.md"]

    def test_subdir_search(self, workdir) -> None:
        root = workdir.container_path
        (root / "p1").mkdir()
        (root / "p2").mkdir()
        (root / "p1" / "x.cfg").write_text("x")
        (root / "p2" / "x.cfg").write_text("x")
        matches, _ = find_files(
            resolve(workdir, "p1"), pattern="*.cfg", limit=50, max_walk_entries=200_000
        )
        assert matches == ["p1/x.cfg"]


class TestLimits:
    def test_limit_early_stop(self, workdir) -> None:
        root = workdir.container_path
        for i in range(10):
            (root / f"m{i}.py").write_text("x")
        matches, truncated = find_files(
            resolve(workdir), pattern="*.py", limit=3, max_walk_entries=200_000
        )
        assert len(matches) == 3
        assert truncated is False
        assert matches == ["m0.py", "m1.py", "m2.py"]

    def test_walk_entry_limit(self, workdir) -> None:
        root = workdir.container_path
        for i in range(10):
            (root / f"m{i}.py").write_text("x")
        matches, truncated = find_files(
            resolve(workdir), pattern="*.py", limit=50, max_walk_entries=5
        )
        assert truncated is True
        assert len(matches) < 10


class TestFiltering:
    def test_hidden_dir_ignored(self, workdir) -> None:
        root = workdir.container_path
        (root / ".git").mkdir()
        (root / ".git" / "config.py").write_text("x")
        matches, _ = find_files(
            resolve(workdir), pattern="*.py", limit=50, max_walk_entries=200_000
        )
        assert matches == []

    def test_symlink_dir_not_followed(self, workdir) -> None:
        root = workdir.container_path
        (root / "realdir").mkdir()
        (root / "realdir" / "inside.py").write_text("x")
        os.symlink("realdir", root / "lnkdir")
        matches, _ = find_files(
            resolve(workdir), pattern="*.py", limit=50, max_walk_entries=200_000
        )
        assert matches == ["realdir/inside.py"]

    def test_denied_file_ignored(self, workdir) -> None:
        root = workdir.container_path
        (root / "secret.key").write_text("x")
        (root / "ok.py").write_text("x")
        matches, _ = find_files(
            resolve(workdir), pattern="*.key", limit=50, max_walk_entries=200_000
        )
        assert matches == []
        matches, _ = find_files(
            resolve(workdir), pattern="*.py", limit=50, max_walk_entries=200_000
        )
        assert matches == ["ok.py"]
