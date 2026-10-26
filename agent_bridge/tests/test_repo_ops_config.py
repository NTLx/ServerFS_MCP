"""Repo Ops policy loading remains explicit, strict, and independent of Agent runtimes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.models import AgentMode


def write_config(tmp_path: Path, workdir: Path, workdir_entry: dict[str, object]) -> Path:
    data = {
        "socket_path": str(tmp_path / "run" / "bridge.sock"),
        "state_dir": str(tmp_path / "state"),
        "lock_dir": str(tmp_path / "locks"),
        "enable_fake_runtime": False,
        "workdirs": [
            {
                "slot": 1,
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "disabled",
                "agent_runtimes": [],
                **workdir_entry,
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_repo_ops_can_be_enabled_without_any_agent_runtime(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = BridgeConfig.load(
        write_config(
            tmp_path,
            repo,
            {
                "repo_ops_enabled": True,
                "repo_actions": {
                    "wiki_lint": {
                        "argv": ["uv", "run", "python", "tools/wiki-lint.py"],
                        "mutates": False,
                        "argument": "none",
                        "integrity": {"tools/wiki-lint.py": "0" * 64},
                    },
                    "registry_ensure": {
                        "argv": ["uv", "run", "python", "tools/compile_registry.py", "ensure"],
                        "mutates": True,
                        "argument": "basename",
                        "integrity": {"tools/compile_registry.py": "0" * 64},
                    },
                },
            },
        )
    )
    policy = config.policies.get("repo")
    assert policy.mode is AgentMode.DISABLED
    assert policy.runtimes == frozenset()
    assert policy.repo_ops_enabled is True
    assert [action.name for action in policy.repo_actions] == ["registry_ensure", "wiki_lint"]
    assert policy.repo_actions[0].argument == "basename"
    assert policy.repo_actions[1].mutates is False


def test_repo_actions_require_explicit_repo_ops_enable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="repo_actions requires repo_ops_enabled"):
        BridgeConfig.load(
            write_config(
                tmp_path,
                repo,
                {
                    "repo_actions": {
                        "wiki_lint": {
                            "argv": ["uv", "run", "python", "tools/wiki-lint.py"],
                            "integrity": {"tools/wiki-lint.py": "0" * 64},
                        }
                    }
                },
            )
        )


def test_read_only_repo_ops_rejects_any_named_action(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = write_config(
        tmp_path,
        repo,
        {
            "read_only": True,
            "repo_ops_enabled": True,
            "repo_actions": {
                "check": {
                    "argv": ["tool", "--check"],
                    "mutates": False,
                    "argument": "none",
                    "integrity": {"guard.txt": "0" * 64},
                }
            },
        },
    )
    with pytest.raises(ValueError, match="read-only workdir cannot configure repo actions"):
        BridgeConfig.load(path)


@pytest.mark.parametrize(
    "actions",
    [
        [],
        {"bad name": {"argv": ["tool"], "integrity": {"guard.txt": "0" * 64}}},
        {"ok": {"argv": [], "integrity": {"guard.txt": "0" * 64}}},
        {"ok": {"argv": ["tool"], "argument": "freeform", "integrity": {"guard.txt": "0" * 64}}},
        {"ok": {"argv": ["tool"], "mutates": "false", "integrity": {"guard.txt": "0" * 64}}},
        {"ok": {"argv": ["tool"], "env": {}, "integrity": {"guard.txt": "0" * 64}}},
        {"ok": {"argv": ["tool"], "integrity": {}}},
        {
            "ok": {
                "argv": ["tool"],
                "integrity": {f"file-{index}.txt": "0" * 64 for index in range(33)},
            }
        },
    ],
)
def test_repo_action_policy_rejects_malformed_or_expansive_shapes(
    tmp_path: Path, actions: object
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        BridgeConfig.load(
            write_config(
                tmp_path,
                repo,
                {"repo_ops_enabled": True, "repo_actions": actions},
            )
        )
