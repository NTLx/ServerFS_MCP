"""Repo Ops private Bridge RPC is strict and independent from Agent task RPC."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.protocol import BridgeProtocolServer


class FakeRepoService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def inspect_repo(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("inspect", params))
        return {"view": params["view"]}

    async def run_repo_action(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("action", params))
        return {"action": params["action"], "returncode": 0}

    async def commit_repo_changes(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("commit", params))
        return {"commit": "abc"}

    async def push_repo(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("push", params))
        return {"pushed": True}


def protocol(tmp_path: Path, service: FakeRepoService) -> BridgeProtocolServer:
    return BridgeProtocolServer(
        service=service,  # type: ignore[arg-type]
        socket_path=tmp_path / "bridge.sock",
    )


@pytest.mark.asyncio
async def test_repo_rpc_dispatches_only_to_repo_service_methods(tmp_path: Path) -> None:
    service = FakeRepoService()
    server = protocol(tmp_path, service)

    await server._dispatch("repo.inspect", {"workdir": "repo", "view": "status", "paths": []})
    await server._dispatch(
        "repo.action",
        {"workdir": "repo", "action": "wiki_lint", "path": None},
    )
    await server._dispatch(
        "repo.commit",
        {"workdir": "repo", "paths": ["wiki/topic.md"], "message": "Update"},
    )
    await server._dispatch("repo.push", {"workdir": "repo"})

    assert service.calls == [
        ("inspect", {"workdir": "repo", "view": "status", "paths": []}),
        ("action", {"workdir": "repo", "action": "wiki_lint", "path": None}),
        ("commit", {"workdir": "repo", "paths": ["wiki/topic.md"], "message": "Update"}),
        ("push", {"workdir": "repo"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("repo.inspect", {"workdir": "repo", "view": "status", "command": "sh"}),
        ("repo.action", {"workdir": "repo", "action": "x", "argv": ["sh"]}),
        ("repo.commit", {"workdir": "repo", "paths": [], "message": "x", "force": True}),
        ("repo.push", {"workdir": "repo", "remote": "origin"}),
        ("repo.push", {"workdir": "repo", "ref": "main"}),
    ],
)
async def test_repo_rpc_rejects_unknown_escape_hatch_fields(
    tmp_path: Path, method: str, params: dict[str, Any]
) -> None:
    service = FakeRepoService()
    server = protocol(tmp_path, service)
    with pytest.raises(BridgeError) as exc:
        await server._dispatch(method, params)
    assert exc.value.code == "INVALID_REQUEST"
    assert service.calls == []


@pytest.mark.asyncio
async def test_repo_rpc_rejects_non_string_paths_before_service(tmp_path: Path) -> None:
    service = FakeRepoService()
    server = protocol(tmp_path, service)
    with pytest.raises(BridgeError) as exc:
        await server._dispatch(
            "repo.commit",
            {"workdir": "repo", "paths": [123], "message": "Update"},
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra", [{"force": True}, {"commit": "a" * 40}, {"workflow": "evil.yml"}, {"ref": "main"}]
)
async def test_release_rpc_rejects_escape_fields(tmp_path: Path, extra: dict) -> None:
    service = FakeRepoService()
    with pytest.raises(BridgeError) as exc:
        await protocol(tmp_path, service)._dispatch(
            "repo.action",
            {"workdir": "repo", "action": "tag_release", "release_tag": "v1.1.0-beta.3", **extra},
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert service.calls == []


@pytest.mark.asyncio
async def test_release_rpc_forwards_structured_tag(tmp_path: Path) -> None:
    service = FakeRepoService()
    await protocol(tmp_path, service)._dispatch(
        "repo.action", {"workdir": "repo", "action": "tag_release", "release_tag": "v1.1.0-beta.3"}
    )
    assert service.calls == [
        (
            "action",
            {
                "workdir": "repo",
                "action": "tag_release",
                "path": None,
                "release_tag": "v1.1.0-beta.3",
            },
        )
    ]
