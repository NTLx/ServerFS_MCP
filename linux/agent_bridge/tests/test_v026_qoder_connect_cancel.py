"""Regression for cancellation before the Qoder SDK handshake completes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from test_qoder_adapter import FakeQoderClient, make_service


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_fails", [False, True])
async def test_cancel_during_sdk_connect_requires_stop_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disconnect_fails: bool
) -> None:
    entered = asyncio.Event()

    class ConnectingClient(FakeQoderClient):
        async def connect(self, prompt: str | None = None) -> None:
            # A child may already exist even though the handshake is not ready.
            assert prompt is None
            self.calls.append("connect")
            self.connected = True
            entered.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.calls.append("disconnect")
            if disconnect_fails:
                raise RuntimeError("synthetic disconnect failure")
            self.connected = False

    service, factory = make_service(tmp_path, monkeypatch, client_type=ConnectingClient)
    await service.start()
    try:
        submitted = await service.submit_task(
            runtime="qoder",
            workdir="repo",
            path="",
            profile="workspace-write",
            prompt="synthetic handshake",
        )
        task_id = submitted["task_id"]
        await asyncio.wait_for(entered.wait(), timeout=5)
        cancelled = await service.cancel_task(task_id)
        assert cancelled["status"] == "cancelled"
        assert service.get_task(task_id)["status"] == "cancelled"
        for _ in range(500):
            if task_id not in service._background:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled handshake did not finish background cleanup")

        client = factory.clients[0]
        assert client.calls == ["connect", "disconnect"]
        assert not client.interrupted  # No ready session to interrupt.
        assert client.connected is disconnect_fails
        policy = service.policies.get("repo")
        guard = service.guard_manager.read(policy.lease_id)
        assert (guard is not None) is disconnect_fails

        if disconnect_fails:
            with pytest.raises(BridgeError) as blocked:
                await service.submit_task(
                    runtime="qoder",
                    workdir="repo",
                    path="",
                    profile="workspace-write",
                    prompt="must not start a second writer",
                )
            assert blocked.value.code == "WORKDIR_RECOVERY_REQUIRED"
            assert len(factory.clients) == 1
        else:
            lease = service.lease_manager.acquire_exclusive(policy.lease_id)
            lease.release()
    finally:
        await service.close()
