from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy, redact_host_path


def policy(root: Path, *, mode: AgentMode, read_only: bool) -> PolicyRegistry:
    return PolicyRegistry(
        [
            WorkdirAgentPolicy(
                slot=1,
                alias="repo",
                host_path=root,
                mode=mode,
                runtimes=frozenset({"fake"}),
                read_only=read_only,
            )
        ]
    )


def test_disabled_is_default_deny(tmp_path: Path) -> None:
    registry = policy(tmp_path, mode=AgentMode.DISABLED, read_only=True)
    with pytest.raises(BridgeError) as exc:
        registry.authorize(workdir="repo", runtime="fake", profile="review", relative_cwd="")
    assert exc.value.code == "AGENT_DISABLED"


def test_review_does_not_allow_workspace_write(tmp_path: Path) -> None:
    registry = policy(tmp_path, mode=AgentMode.REVIEW, read_only=True)
    with pytest.raises(BridgeError) as exc:
        registry.authorize(
            workdir="repo",
            runtime="fake",
            profile="workspace-write",
            relative_cwd="",
        )
    assert exc.value.code == "AGENT_PROFILE_NOT_ALLOWED"


def test_workspace_write_requires_writable_workdir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a writable"):
        WorkdirAgentPolicy(
            slot=1,
            alias="repo",
            host_path=tmp_path,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"fake"}),
            read_only=True,
        )


def test_relative_cwd_confined_to_root(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    registry = policy(tmp_path, mode=AgentMode.WORKSPACE_WRITE, read_only=False)
    _, cwd = registry.authorize(
        workdir="repo", runtime="fake", profile="review", relative_cwd="sub"
    )
    assert cwd == sub.resolve()
    _, root_cwd = registry.authorize(
        workdir="repo", runtime="fake", profile="review", relative_cwd=""
    )
    assert root_cwd == tmp_path.resolve()

    with pytest.raises(BridgeError) as exc:
        registry.authorize(
            workdir="repo", runtime="fake", profile="review", relative_cwd="../outside"
        )
    assert exc.value.code == "INVALID_WORKDIR_PATH"

    absolute = str(tmp_path / "sub")
    with pytest.raises(BridgeError) as exc:
        registry.authorize(workdir="repo", runtime="fake", profile="review", relative_cwd=absolute)
    assert exc.value.code == "INVALID_WORKDIR_PATH"

    with pytest.raises(BridgeError) as exc:
        registry.authorize(workdir="repo", runtime="fake", profile="review", relative_cwd="missing")
    assert exc.value.code == "WORKDIR_PATH_NOT_FOUND"

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        registry.authorize(workdir="repo", runtime="fake", profile="review", relative_cwd="file")
    assert exc.value.code == "INVALID_WORKDIR_PATH"

    with pytest.raises(BridgeError) as exc:
        registry.authorize(
            workdir="repo", runtime="fake", profile="review", relative_cwd="bad\x00path"
        )
    assert exc.value.code == "INVALID_WORKDIR_PATH"


def test_host_path_redaction(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    assert redact_host_path(tmp_path, tmp_path) == "."
    assert redact_host_path(tmp_path, sub / "file.txt") == "sub/file.txt"
    assert redact_host_path(tmp_path, "sub/file.txt") == "sub/file.txt"
    assert redact_host_path(tmp_path, tmp_path.parent / "secret.txt") == "<outside-workdir>"


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    registry = policy(tmp_path, mode=AgentMode.REVIEW, read_only=True)
    with pytest.raises(BridgeError) as exc:
        registry.authorize(workdir="repo", runtime="fake", profile="review", relative_cwd="escape")
    assert exc.value.code == "INVALID_WORKDIR_PATH"

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BridgeError) as exc:
        registry.authorize(
            workdir="repo", runtime="fake", profile="review", relative_cwd="nested/escape"
        )
    assert exc.value.code == "INVALID_WORKDIR_PATH"


def test_duplicate_slots_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate workdir slot"):
        PolicyRegistry(
            [
                WorkdirAgentPolicy(1, "one", tmp_path, runtimes=frozenset({"fake"})),
                WorkdirAgentPolicy(1, "two", tmp_path, runtimes=frozenset({"fake"})),
            ]
        )
