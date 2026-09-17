"""Workdir registry validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.workdirs import (
    DISABLED_SENTINEL,
    SLOT_COUNT,
    WorkdirError,
    build_registry,
)


def make_root(tmp_path: Path, enabled: dict[int, bool] | None = None) -> Path:
    """Create a 16-slot root; enabled slots have real dirs, others sentinels."""
    root = tmp_path / "workdirs"
    root.mkdir()
    for slot in range(1, SLOT_COUNT + 1):
        d = root / f"{slot:02d}"
        d.mkdir()
        if not (enabled or {}).get(slot, False):
            (d / DISABLED_SENTINEL).touch()
    return root


def envs(alias: str = "", **overrides: str) -> tuple[dict[int, str], dict[int, str]]:
    aliases = {s: "" for s in range(1, SLOT_COUNT + 1)}
    if alias:
        aliases[1] = alias
    return aliases, {s: "" for s in range(1, SLOT_COUNT + 1)}


class TestValidConfig:
    def test_valid_alias(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert reg.get("projects") is not None

    @pytest.mark.parametrize(
        "alias",
        ["logs", "app-logs", "bioinfo", "paper_db", "ProjectA", "a" * 32],
    )
    def test_alias_shapes(self, tmp_path: Path, alias: str) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs(alias), workdir_root=root)
        assert reg.get(alias) is not None

    def test_description_round_trip(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, _ = envs("projects")
        reg = build_registry(
            aliases, {1: "项目代码", **{s: "" for s in range(2, 17)}}, workdir_root=root
        )
        assert reg.list_result().workdirs[0].description == "项目代码"

    def test_description_empty_becomes_none(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert reg.list_result().workdirs[0].description is None


class TestInvalidAlias:
    @pytest.mark.parametrize(
        "alias",
        ["/foo", "../foo", "foo/bar", "foo bar", "123project", "", "a" * 33, "中文"],
    )
    def test_invalid_alias_fails(self, tmp_path: Path, alias: str) -> None:
        root = make_root(tmp_path, {1: True})
        if not alias:
            # empty alias + real dir = case C, different error but still fatal
            with pytest.raises(WorkdirError):
                build_registry(*envs(""), workdir_root=root)
            return
        with pytest.raises(WorkdirError, match="invalid alias"):
            build_registry(*envs(alias), workdir_root=root)

    def test_duplicate_alias_fails(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        aliases, _ = envs("projects")
        aliases[2] = "projects"
        with pytest.raises(WorkdirError, match="duplicate alias"):
            build_registry(aliases, _, workdir_root=root)


class TestSlotStates:
    def test_disabled_normal(self, tmp_path: Path) -> None:
        """Case A: empty alias + sentinel -> OK, slot skipped."""
        root = make_root(tmp_path)  # all disabled
        reg = build_registry(*envs(), workdir_root=root)
        assert len(reg) == 0

    def test_alias_set_with_sentinel_fails(self, tmp_path: Path) -> None:
        """Case B: alias set + sentinel -> startup error."""
        root = make_root(tmp_path)  # slot 1 has sentinel
        with pytest.raises(WorkdirError, match="disabled"):
            build_registry(*envs("projects"), workdir_root=root)

    def test_alias_empty_with_real_dir_fails(self, tmp_path: Path) -> None:
        """Case C: alias empty + real mounted dir -> startup error."""
        root = make_root(tmp_path, {1: True})
        with pytest.raises(WorkdirError, match="alias is empty"):
            build_registry(*envs(""), workdir_root=root)

    def test_enabled_workdir(self, tmp_path: Path) -> None:
        """Case D: alias + real dir -> enabled."""
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert len(reg) == 1
        assert reg.get("projects") is not None

    def test_all_16_slots_configured(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {s: True for s in range(1, 17)})
        aliases = {s: f"dir{s:02d}" for s in range(1, 17)}
        reg = build_registry(aliases, {s: "" for s in range(1, 17)}, workdir_root=root)
        assert len(reg) == 16

    def test_reserved_sentinel_in_real_workdir_fails(self, tmp_path: Path) -> None:
        """Real mounted dir containing .serverfs-disabled -> conflict."""
        root = make_root(tmp_path, {1: True})
        (root / "01" / DISABLED_SENTINEL).touch()
        (root / "01" / "other.txt").touch()
        with pytest.raises(WorkdirError, match="reserved"):
            build_registry(*envs("projects"), workdir_root=root)
