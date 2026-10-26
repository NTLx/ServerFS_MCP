"""Per-workdir Repo Ops authorization is explicit and fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import WorkdirError, build_registry_from_env


def slot_root(tmp_path: Path) -> Path:
    root = tmp_path / "workdirs"
    for slot in range(1, 17):
        slot_path = root / f"{slot:02d}"
        slot_path.mkdir(parents=True)
        if slot != 1:
            (slot_path / ".serverfs-disabled").touch()
    return root


def test_repo_ops_defaults_off_and_can_be_enabled_per_workdir(tmp_path: Path) -> None:
    root = slot_root(tmp_path)
    base = {
        "WORKDIR_01_ALIAS": "repo",
        "WORKDIR_01_READ_ONLY": "false",
    }
    registry = build_registry_from_env(base, Settings(), workdir_root=root)
    assert registry.get("repo").policy.repo_ops_enabled is False  # type: ignore[union-attr]

    enabled = dict(base)
    enabled["WORKDIR_01_REPO_OPS_ENABLED"] = "true"
    registry = build_registry_from_env(enabled, Settings(), workdir_root=root)
    assert registry.get("repo").policy.repo_ops_enabled is True  # type: ignore[union-attr]
    result = registry.list_result()
    assert result.workdirs[0].repo_ops is True


def test_disabled_slot_cannot_enable_repo_ops(tmp_path: Path) -> None:
    root = tmp_path / "workdirs"
    slot = root / "01"
    slot.mkdir(parents=True)
    (slot / ".serverfs-disabled").touch()
    with pytest.raises(WorkdirError, match="disabled slot cannot enable Repo Ops"):
        build_registry_from_env(
            {"WORKDIR_01_REPO_OPS_ENABLED": "true"},
            Settings(),
            workdir_root=root,
        )


def test_invalid_repo_ops_boolean_fails_closed(tmp_path: Path) -> None:
    root = slot_root(tmp_path)
    with pytest.raises(WorkdirError, match="REPO_OPS_ENABLED"):
        build_registry_from_env(
            {
                "WORKDIR_01_ALIAS": "repo",
                "WORKDIR_01_REPO_OPS_ENABLED": "maybe",
            },
            Settings(),
            workdir_root=root,
        )
