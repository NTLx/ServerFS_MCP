"""Repo Ops service integration: shared writer lease and no provider task path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from platform_contract import WINDOWS
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, RepoActionPolicy, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


class FakeRepoOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def inspect(self, policy, *, view: str, paths: list[str]) -> dict[str, Any]:
        self.calls.append(("inspect", {"view": view, "paths": paths}))
        return {"view": view}

    async def run_action(self, policy, *, action_name: str, path: str | None) -> dict[str, Any]:
        self.calls.append(("action", {"action": action_name, "path": path}))
        action = next(item for item in policy.repo_actions if item.name == action_name)
        return {"action": action_name, "mutates": action.mutates, "returncode": 0}

    async def commit(self, policy, *, paths: list[str], message: str) -> dict[str, Any]:
        self.calls.append(("commit", {"paths": paths, "message": message}))
        return {"commit": "abc", "paths": paths}

    async def push(self, policy) -> dict[str, Any]:
        self.calls.append(("push", {}))
        return {"pushed": True}


def make_service(tmp_path: Path) -> tuple[BridgeService, WorkdirAgentPolicy]:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkdirAgentPolicy(
        slot=None if WINDOWS else 1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.DISABLED,
        runtimes=frozenset(),
        read_only=False,
        repo_ops_enabled=True,
        repo_actions=(
            RepoActionPolicy(
                name="readcheck",
                argv=("tool",),
                mutates=False,
                integrity=(
                    (
                        "guard.txt",
                        "0000000000000000000000000000000000000000000000000000000000000000",
                    ),
                ),
            ),
            RepoActionPolicy(
                name="fix",
                argv=("tool", "--fix"),
                mutates=True,
                integrity=(
                    (
                        "guard.txt",
                        "0000000000000000000000000000000000000000000000000000000000000000",
                    ),
                ),
            ),
        ),
    )
    service = BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry([policy]),
        adapters={},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id]),
    )
    service.repo_ops = FakeRepoOps()  # type: ignore[assignment]
    return service, policy


@pytest.mark.asyncio
async def test_repo_inspection_and_read_action_do_not_require_or_start_agent_runtime(
    tmp_path: Path,
) -> None:
    service, _policy = make_service(tmp_path)
    result = await service.inspect_repo(workdir="repo", view="status", paths=[])
    assert result == {"view": "status"}
    action = await service.run_repo_action(workdir="repo", action="readcheck", path=None)
    assert action["returncode"] == 0
    assert service.adapters == {}
    assert service._background == {}


@pytest.mark.asyncio
async def test_mutating_repo_ops_share_exclusive_writer_lease(tmp_path: Path) -> None:
    service, policy = make_service(tmp_path)
    other = LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id])
    held = other.acquire_exclusive(policy.lease_id)
    try:
        with pytest.raises(BridgeError) as exc:
            await service.commit_repo_changes(
                workdir="repo",
                paths=["wiki/topic.md"],
                message="Update",
            )
        assert exc.value.code == "WORKDIR_BUSY"
        assert service.repo_ops.calls == []  # type: ignore[attr-defined]
    finally:
        held.release()

    result = await service.commit_repo_changes(
        workdir="repo",
        paths=["wiki/topic.md"],
        message="Update",
    )
    assert result["commit"] == "abc"


@pytest.mark.asyncio
async def test_all_named_actions_take_writer_lease(tmp_path: Path) -> None:
    service, policy = make_service(tmp_path)
    other = LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id])
    held = other.acquire_exclusive(policy.lease_id)
    try:
        for action in ("readcheck", "fix"):
            with pytest.raises(BridgeError) as exc:
                await service.run_repo_action(workdir="repo", action=action, path=None)
            assert exc.value.code == "WORKDIR_BUSY"
        assert service.repo_ops.calls == []  # type: ignore[attr-defined]
    finally:
        held.release()

    read_result = await service.run_repo_action(
        workdir="repo",
        action="readcheck",
        path=None,
    )
    assert read_result["returncode"] == 0
