"""Linux named signed-v2 multi-alias Core binding and dispatch contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeRemoteError
from serverfs_mcp.core_bridge_client_v2 import CoreBridgeRemoteError
from serverfs_mcp.linux_v0250_core_binding import (
    LinuxCoreBindingError,
    create_linux_v0250_agent_adapter,
    load_linux_core_client,
)
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux multi-workdir binding")


def _fixture(tmp_path: Path):
    file = tmp_path / "core-bridge-binding.json"
    document = {
        "schema_version": 2,
        "instance_id": "core-a",
        "core_id": "core-a",
        "bridge_id": "bridge-a",
        "key_id": "key-a",
        "secret_hex": bytes(range(32)).hex(),
        "workdirs": [
            {"alias": "Repo", "workdir_path": "/workdirs/01", "policy_revision": "a" * 64},
            {"alias": "Docs", "workdir_path": "/workdirs/03", "policy_revision": "b" * 64},
        ],
        "socket_path": str(tmp_path / "bridge.sock"),
    }
    file.write_text(json.dumps(document), encoding="utf-8")
    file.chmod(0o600)
    workdirs = [
        Workdir("Repo", Path("/workdirs/01"), None, policy=EffectiveWorkdirPolicy(
            agent_mode="workspace-write", agent_runtimes=frozenset({"claude"})
        )),
        Workdir("Docs", Path("/workdirs/03"), None, policy=EffectiveWorkdirPolicy(
            agent_mode="disabled", repo_ops_enabled=True
        )),
    ]
    return file, document, workdirs


def _load(file, workdirs):
    return load_linux_core_client(
        str(file),
        instance_id="core-a",
        bridge_id="bridge-a",
        socket_path=str(file.parent / "bridge.sock"),
        workdirs=workdirs,
    )


def test_two_aliases_bind_distinct_slots_and_policy_revisions(tmp_path: Path) -> None:
    file, document, workdirs = _fixture(tmp_path)
    clients, alias = _load(file, workdirs)
    assert alias is None
    assert set(clients) == {"Repo", "Docs"}
    assert clients["Repo"]._policy_revision == "a" * 64
    assert clients["Docs"]._policy_revision == "b" * 64
    assert clients["Repo"]._secret == clients["Docs"]._secret
    assert document["secret_hex"] not in repr(clients["Repo"])


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_alias",
        "duplicate_slot",
        "outside_slot",
        "missing_policy_revision",
        "extra_alias",
        "missing_alias",
        "path_mismatch",
        "wrong_version",
    ],
)
def test_multi_alias_binding_fails_closed(tmp_path: Path, mutation: str) -> None:
    file, document, workdirs = _fixture(tmp_path)
    if mutation == "duplicate_alias":
        document["workdirs"][1]["alias"] = "Repo"
    elif mutation == "duplicate_slot":
        document["workdirs"][1]["workdir_path"] = "/workdirs/01"
    elif mutation == "outside_slot":
        document["workdirs"][1]["workdir_path"] = "/workdirs/17"
    elif mutation == "missing_policy_revision":
        del document["workdirs"][1]["policy_revision"]
    elif mutation == "extra_alias":
        document["workdirs"].append(
            {"alias": "Other", "workdir_path": "/workdirs/04", "policy_revision": "c" * 64}
        )
    elif mutation == "missing_alias":
        document["workdirs"].pop()
    elif mutation == "path_mismatch":
        document["workdirs"][1]["workdir_path"] = "/workdirs/02"
    elif mutation == "wrong_version":
        document["schema_version"] = 1
    file.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LinuxCoreBindingError):
        _load(file, workdirs)


class _SignedClient:
    def __init__(self, alias: str, owned: str = "Docs") -> None:
        self._workdir_alias = alias
        self.owned = owned
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "task.get" and self._workdir_alias != self.owned:
            raise CoreBridgeRemoteError("CORE_TASK_NOT_FOUND", "Core task is not available")
        return {"method": method, "alias": self._workdir_alias}


def _adapter():
    repo, docs = _SignedClient("Repo"), _SignedClient("Docs")
    return repo, docs, create_linux_v0250_agent_adapter({"Repo": repo, "Docs": docs}, None)


def test_explicit_workdir_routing_and_foreign_alias_denial() -> None:
    repo, docs, adapter = _adapter()
    result = asyncio.run(adapter.call("repo.inspect", {"workdir": "Docs", "view": "status"}))
    assert result["alias"] == "Docs"
    assert docs.calls == [("repo.inspect", {"view": "status"})]
    assert not repo.calls
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(adapter.call("repo.inspect", {"workdir": "Foreign", "view": "status"}))


def test_task_only_lookup_resolves_signed_owner_without_guessing() -> None:
    repo, docs, adapter = _adapter()
    result = asyncio.run(adapter.call("task.message.send", {"task_id": "agt_1", "message": "next"}))
    assert result["alias"] == "Docs"
    assert repo.calls == [("task.get", {"task_id": "agt_1"})]
    assert docs.calls == [
        ("task.get", {"task_id": "agt_1"}),
        ("task.message.send", {"task_id": "agt_1", "message": "next"}),
    ]


def test_unknown_task_is_not_disclosed() -> None:
    one, two = _SignedClient("Repo", owned="Other"), _SignedClient("Docs", owned="Other")
    adapter = create_linux_v0250_agent_adapter({"Repo": one, "Docs": two}, None)
    with pytest.raises(AgentBridgeRemoteError) as error:
        asyncio.run(adapter.call("task.get", {"task_id": "missing"}))
    assert error.value.code == "CORE_TASK_NOT_FOUND"


def test_multi_alias_requires_explicit_submit_target() -> None:
    _, _, adapter = _adapter()
    with pytest.raises(AgentBridgeClientError):
        asyncio.run(adapter.call("task.submit", {"runtime": "claude", "prompt": "test"}))
