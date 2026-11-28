"""Core/Bridge v2 alias root and execution-policy parity, fail closed.

A signed Bridge policy digest authenticates the Bridge's own host root.  A
separate Core may not bind its public file tools to a different root under
the same alias, even when the HMAC capability itself is well formed.
These tests use temp paths and never access a Windows service or credential.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25.core_bridge_binding import (
    CoreBridgeBindingError,
    verify_core_bridge_workdirs,
)


def _core(
    root: Path,
    *,
    alias="Repo",
    read_only=False,
    mode="review",
    runtimes=frozenset({"codex"}),
    repo_ops=False,
):
    return SimpleNamespace(
        alias=alias,
        root=root,
        read_only=read_only,
        agent_mode=mode,
        agent_runtimes=runtimes,
        policy=SimpleNamespace(repo_ops_enabled=repo_ops),
    )


def _bridge(root: Path) -> dict:
    return {
        "workdirs": [
            {
                "alias": "Repo",
                "host_path": str(root),
                "read_only": False,
                "agent_mode": "review",
                "agent_runtimes": ["codex"],
                "repo_ops_enabled": False,
            },
            {
                "alias": "Other",
                "host_path": str(root),
                "read_only": True,
                "agent_mode": "disabled",
            },
        ]
    }


def _verify(
    cores: list, document: dict, *, canonicalize=lambda raw: Path(raw).resolve(strict=True)
):
    return verify_core_bridge_workdirs(cores, document, canonicalize=canonicalize)


def test_exact_shared_bridge_alias_scope_can_be_a_subset(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert _verify([_core(root)], _bridge(root)) is None


def test_same_alias_with_different_host_root_denied(tmp_path: Path) -> None:
    core_root = tmp_path / "core"
    bridge_root = tmp_path / "bridge"
    core_root.mkdir()
    bridge_root.mkdir()
    with pytest.raises(CoreBridgeBindingError, match="invalid or not private"):
        _verify([_core(core_root)], _bridge(bridge_root))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["workdirs"][0].update(read_only=True),
        lambda d: d["workdirs"][0].update(read_only="false"),
        lambda d: d["workdirs"][0].update(agent_mode="disabled"),
        lambda d: d["workdirs"][0].update(agent_mode="workspace-write"),
        lambda d: d["workdirs"][0].update(agent_runtimes=["claude"]),
        lambda d: d["workdirs"][0].update(agent_runtimes=["codex", "codex"]),
        lambda d: d["workdirs"][0].update(agent_runtimes="codex"),
        lambda d: d["workdirs"][0].update(repo_ops_enabled=True),
        lambda d: d["workdirs"][0].update(repo_ops_enabled="false"),
        lambda d: d["workdirs"][0].pop("host_path"),
        lambda d: d["workdirs"][0].update(host_path=""),
        lambda d: d["workdirs"][0].update(alias="Elsewhere"),
    ],
)
def test_alias_scope_execution_policy_or_path_drift_is_rejected(tmp_path: Path, change) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    document = _bridge(root)
    change(document)
    with pytest.raises(CoreBridgeBindingError):
        _verify([_core(root)], document)


def test_writable_repo_ops_same_root_requires_same_enablement(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    document = _bridge(root)
    document["workdirs"][0].update(agent_mode="disabled", agent_runtimes=[], repo_ops_enabled=True)
    assert (
        _verify([_core(root, mode="disabled", runtimes=frozenset(), repo_ops=True)], document)
        is None
    )


def test_disabled_core_workdir_is_not_an_agent_binding(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    document = _bridge(root)
    disabled = _core(
        other, alias="Unrelated", read_only=True, mode="disabled", runtimes=frozenset()
    )
    assert _verify([_core(root), disabled], document) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["workdirs"].append(copy.deepcopy(d["workdirs"][0])),
        lambda d: d.update(workdirs=[]),
        lambda d: d.update(workdirs="bad"),
        lambda d: d["workdirs"].append({"alias": "../invalid", "host_path": "x"}),
        lambda d: d["workdirs"].append({"host_path": "x"}),
    ],
)
def test_invalid_bridge_workdir_inventory_cannot_be_trusted(tmp_path: Path, change) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    document = _bridge(root)
    change(document)
    with pytest.raises(CoreBridgeBindingError):
        _verify([_core(root)], document)


def test_duplicate_local_alias_or_no_active_bridge_scope_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(CoreBridgeBindingError):
        _verify([_core(root), _core(root)], _bridge(root))
    with pytest.raises(CoreBridgeBindingError):
        _verify([_core(root, mode="disabled", runtimes=frozenset())], _bridge(root))


def test_invalid_canonical_path_or_reparse_detection_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    def denied(_raw):
        raise OSError("possible redirected junction with secret path")

    with pytest.raises(CoreBridgeBindingError) as caught:
        _verify([_core(root)], _bridge(root), canonicalize=denied)
    assert "secret path" not in str(caught.value)
