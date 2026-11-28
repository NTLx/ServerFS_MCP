"""Shared Bridge capability digest must bind the actual workdir policy."""

from __future__ import annotations

import re
from pathlib import Path

from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.manifest import policy_fingerprint
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import RepoActionPolicy, WorkdirAgentPolicy


def _policy(
    root: Path,
    *,
    alias: str = "Repo",
    writable: bool = False,
    repo_enabled: bool = False,
    actions: tuple[RepoActionPolicy, ...] = (),
) -> WorkdirAgentPolicy:
    return WorkdirAgentPolicy(
        slot=None,
        alias=alias,
        host_path=root,
        mode=AgentMode.DISABLED,
        read_only=not writable,
        repo_ops_enabled=repo_enabled,
        repo_actions=actions,
    )


def test_v2_digest_binds_canonical_root_and_alias_without_exposure(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = _policy(first)
    b = _policy(second)
    c = _policy(first, alias="Other")
    digest = core_workdir_policy_digest(a)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert str(first) not in digest
    assert digest == core_workdir_policy_digest(_policy(first))
    assert digest != core_workdir_policy_digest(b)
    assert digest != core_workdir_policy_digest(c)


def test_v2_digest_covers_write_and_repo_ops_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    read_only = _policy(root)
    writable = _policy(root, writable=True)
    repo = _policy(root, writable=True, repo_enabled=True)
    assert core_workdir_policy_digest(read_only) != core_workdir_policy_digest(writable)
    assert core_workdir_policy_digest(writable) != core_workdir_policy_digest(repo)


def test_v2_digest_covers_exact_repo_action_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    action = RepoActionPolicy(
        name="status",
        argv=("git", "status"),
        integrity=(("README.md", "a" * 64),),
    )
    variant = RepoActionPolicy(
        name="status",
        argv=("git", "status", "--short"),
        integrity=(("README.md", "a" * 64),),
    )
    first = _policy(root, writable=True, repo_enabled=True, actions=(action,))
    second = _policy(root, writable=True, repo_enabled=True, actions=(variant,))
    assert core_workdir_policy_digest(first) != core_workdir_policy_digest(second)


def test_legacy_manifest_fingerprint_is_not_sufficient_for_v2_grants(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    a = _policy(root_a)
    b = _policy(root_b)
    assert policy_fingerprint(a) == policy_fingerprint(b)
    assert core_workdir_policy_digest(a) != core_workdir_policy_digest(b)
