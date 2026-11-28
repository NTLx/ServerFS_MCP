"""Authenticated v2 task submissions persist their Core owner atomically."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.store import TaskStore
from test_core_grants_protocol import _dispatch, _server, _signed


@pytest.mark.asyncio
async def test_authenticated_submit_persists_owner_and_blocks_other_core(tmp_path: Path) -> None:
    server, store = _server(tmp_path)
    request = _signed(
        server,
        "task.submit",
        nonce="1" * 32,
        params={
            "runtime": "fake",
            "workdir": "Repo",
            "path": ".",
            "profile": "review",
            "prompt": "synthetic",
        },
    )

    response = await _dispatch(server, request)
    assert response["ok"] is True
    task_id = response["result"]["task_id"]
    store.require_core_task_owner(task_id, core_id="alpha", workdir_alias="Repo")

    reopened = TaskStore(tmp_path / "state")
    reopened.require_core_task_owner(task_id, core_id="alpha", workdir_alias="Repo")
    with pytest.raises(BridgeError, match="Core task is not available"):
        reopened.require_core_task_owner(task_id, core_id="beta", workdir_alias="Repo")


@pytest.mark.asyncio
async def test_v2_submit_rejects_workdir_mismatch_continuation_and_idempotency(
    tmp_path: Path,
) -> None:
    server, _ = _server(tmp_path)
    common = {
        "runtime": "fake",
        "workdir": "Repo",
        "path": ".",
        "profile": "review",
        "prompt": "synthetic",
    }

    mismatch = _signed(
        server,
        "task.submit",
        nonce="2" * 32,
        params={**common, "workdir": "Other"},
    )
    assert (await _dispatch(server, mismatch))["ok"] is False

    continuation = _signed(
        server,
        "task.submit",
        nonce="3" * 32,
        params={**common, "continue_from_task_id": "agt_some_task"},
    )
    assert (await _dispatch(server, continuation))["ok"] is False

    idempotent = _signed(
        server,
        "task.submit",
        nonce="4" * 32,
        params={**common, "idempotency_key": "cross-core-key"},
    )
    assert (await _dispatch(server, idempotent))["ok"] is False


@pytest.mark.asyncio
async def test_v1_submit_behavior_unchanged_and_internal_identity_cannot_be_spoofed(
    tmp_path: Path,
) -> None:
    server, store = _server(tmp_path, v2_only=False)
    response = await _dispatch(
        server,
        {
            "protocol_version": 1,
            "request_id": "legacy-submit",
            "method": "task.submit",
            "params": {
                "runtime": "fake",
                "workdir": "Repo",
                "path": ".",
                "profile": "review",
                "prompt": "legacy synthetic",
                "_verified_owner_core_id": "alpha",
            },
        },
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_REQUEST"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    legacy = await _dispatch(
        server,
        {
            "protocol_version": 1,
            "request_id": "legacy-submit-valid",
            "method": "task.submit",
            "params": {
                "runtime": "fake",
                "workdir": "Repo",
                "path": ".",
                "profile": "review",
                "prompt": "legacy synthetic",
            },
        },
    )
    assert legacy["ok"] is True
    with pytest.raises(BridgeError, match="Core task is not available"):
        store.require_core_task_owner(
            legacy["result"]["task_id"], core_id="alpha", workdir_alias="Repo"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_owner", ["", "UPPERCASE", "../other", "a" * 33])
async def test_invalid_core_owner_is_rejected_before_submission_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_owner: str
) -> None:
    server, store = _server(tmp_path)

    def unexpected_gc() -> None:
        pytest.fail("invalid Core identity reached submission side effects")

    monkeypatch.setattr(server.service, "_gc_retained", unexpected_gc)
    with pytest.raises(BridgeError) as caught:
        await server.service.submit_task(
            runtime="fake",
            workdir="Repo",
            path=".",
            profile="review",
            prompt="synthetic",
            owner_core_id=invalid_owner,
        )
    assert caught.value.code == "CORE_NOT_AUTHORIZED"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
