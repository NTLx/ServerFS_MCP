"""Actual Windows Pipe + ClaudeAdapter lifecycle + two Core HMAC task ownership.

Uses the real ClaudeAdapter implementation, but its Claude SDK client is the
deterministic fake client from test_claude_adapter. No real Claude Code CLI,
provider API, public network, installed ServerFS tasks, config or credentials.
Bridge's temporary SQLite store, leases and random Windows Named Pipe are real.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import pytest

from pipe_support import PipeClient, RunningBridge, new_pipe_name, wait_until
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.adapters.claude import ClaudeAdapter
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest, sign_core_request
from serverfs_agent_bridge.core_grants_private import (
    load_private_core_grants,
    provision_private_core_grants,
)
from test_claude_adapter import make_service

require_windows_kernel("requires real Windows Named Pipe and lease backend")

BRIDGE = "claude-adapter-pipe-fixture"
METHODS = [
    "bridge.ping",
    "runtime.list",
    "task.submit",
    "task.get",
    "task.events",
    "task.result.read",
    "task.cancel",
]


def _request(
    core_id: str, key_id: str, secret: bytes, digest: str, method: str, params: dict
) -> bytes:
    unsigned = {
        "protocol_version": 2,
        "bridge_id": BRIDGE,
        "core_id": core_id,
        "key_id": key_id,
        "request_id": "req-" + secrets.token_hex(9),
        "method": method,
        "params": params,
        "policy_revision": digest,
        "issued_at": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    return (
        json.dumps(
            {**unsigned, "signature": sign_core_request(unsigned, secret)},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _call(
    pipe: PipeClient,
    core_id: str,
    key_id: str,
    secret: bytes,
    digest: str,
    method: str,
    params: dict,
) -> dict:
    pipe.write(_request(core_id, key_id, secret, digest, method, params))
    return pipe.read_frame()


def test_real_windows_pipe_claude_adapter_core_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client_factory = make_service(tmp_path, monkeypatch)
    assert isinstance(service.adapters["claude"], ClaudeAdapter)
    digest = core_workdir_policy_digest(service.policies.get("repo"))
    keys = {
        "core-a": ("key-a", secrets.token_bytes(32)),
        "core-b": ("key-b", secrets.token_bytes(32)),
    }
    grant_doc = {
        "schema_version": 1,
        "bridge_id": BRIDGE,
        "grants": [
            {
                "core_id": core,
                "key_id": pair[0],
                "secret_hex": pair[1].hex(),
                "methods": METHODS,
                "workdirs": [{"alias": "repo", "policy_sha256": digest}],
            }
            for core, pair in keys.items()
        ],
    }
    provision_private_core_grants(
        store=service.store,
        bridge_id=BRIDGE,
        policies=service.policies,
        document=grant_doc,
    )
    verifier = load_private_core_grants(
        store=service.store, bridge_id=BRIDGE, policies=service.policies
    )
    assert verifier.durable_replay_protection

    def call(pipe: PipeClient, core: str, method: str, params: dict) -> dict:
        key_id, secret = keys[core]
        return _call(pipe, core, key_id, secret, digest, method, params)

    with RunningBridge(service, pipe_name=new_pipe_name(), core_grants=verifier) as running:
        with PipeClient(running.pipe_name) as pipe:
            assert call(pipe, "core-a", "bridge.ping", {"workdir": "repo"})["ok"]

            task_ids: dict[str, str] = {}
            for core in ("core-a", "core-b"):
                submitted = call(
                    pipe,
                    core,
                    "task.submit",
                    {
                        "runtime": "claude",
                        "workdir": "repo",
                        "path": "",
                        "profile": "workspace-write",
                        "prompt": "isolated-response-" + core,
                    },
                )
                assert submitted["ok"] is True
                task_id = submitted["result"]["task_id"]
                task_ids[core] = task_id
                assert wait_until(
                    lambda task_id=task_id: (
                        service.store.get_task(task_id).status
                        in {"succeeded", "failed", "cancelled", "interrupted", "timed_out"}
                    ),
                    timeout_seconds=15,
                )
                terminal = service.store.get_task(task_id)
                assert terminal.status == "succeeded", (terminal.status, terminal.error_code)
                own = call(
                    pipe,
                    core,
                    "task.get",
                    {"workdir": "repo", "task_id": task_id},
                )
                assert own["ok"] is True
                assert own["result"]["task_id"] == task_id
                assert own["result"]["final_response"] == "isolated-response-" + core
                foreign = "core-b" if core == "core-a" else "core-a"
                for method in (
                    "task.get",
                    "task.events",
                    "task.result.read",
                    "task.cancel",
                ):
                    denied = call(
                        pipe,
                        foreign,
                        method,
                        {"workdir": "repo", "task_id": task_id},
                    )
                    assert denied["ok"] is False
                    assert denied["error"]["code"] == "CORE_TASK_NOT_FOUND"

            assert task_ids["core-a"] != task_ids["core-b"]
            assert len(client_factory.clients) == 2
            assert all(client.connected is False for client in client_factory.clients)


def test_real_windows_pipe_claude_partial_message_then_owner_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel only the owning Core after a fake SDK init and text block.

    All IPC, HMAC, SQLite and lease operations are native Windows; only the
    Claude SDK client is fake, so no model, file tool or provider is invoked.
    """
    import asyncio

    import serverfs_agent_bridge.adapters.claude as claude_module
    from test_claude_adapter import (
        FakeAssistantMessage,
        FakeClaudeClient,
        FakeResultMessage,
        FakeTextBlock,
    )

    service, factory = make_service(tmp_path, monkeypatch)

    class PartialStreamClient(FakeClaudeClient):
        async def receive_response(self):
            assert self.prompt == "simulated-partial-stream"
            yield claude_module.SystemMessage(
                subtype="init", data={"session_id": "native-partial-session"}
            )
            yield FakeAssistantMessage([FakeTextBlock("partial-stream-evidence")])
            while not self.interrupted:
                await asyncio.sleep(0.01)
            yield FakeResultMessage(
                result="interrupted",
                session_id="native-partial-session",
                terminal_reason="aborted_streaming",
            )

    def create_client(options):
        client = PartialStreamClient(options)
        factory.clients.append(client)
        return client

    service.adapters["claude"]._client_factory = create_client
    digest = core_workdir_policy_digest(service.policies.get("repo"))
    keys = {
        "core-a": ("key-a", secrets.token_bytes(32)),
        "core-b": ("key-b", secrets.token_bytes(32)),
    }
    grants = {
        "schema_version": 1,
        "bridge_id": BRIDGE,
        "grants": [
            {
                "core_id": core,
                "key_id": pair[0],
                "secret_hex": pair[1].hex(),
                "methods": METHODS,
                "workdirs": [{"alias": "repo", "policy_sha256": digest}],
            }
            for core, pair in keys.items()
        ],
    }
    provision_private_core_grants(
        store=service.store,
        bridge_id=BRIDGE,
        policies=service.policies,
        document=grants,
    )
    verifier = load_private_core_grants(
        store=service.store, bridge_id=BRIDGE, policies=service.policies
    )

    def call(pipe: PipeClient, core: str, method: str, params: dict) -> dict:
        key_id, secret = keys[core]
        return _call(pipe, core, key_id, secret, digest, method, params)

    with RunningBridge(service, pipe_name=new_pipe_name(), core_grants=verifier) as running:
        with PipeClient(running.pipe_name) as pipe:
            submitted = call(
                pipe,
                "core-a",
                "task.submit",
                {
                    "runtime": "claude",
                    "workdir": "repo",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": "simulated-partial-stream",
                },
            )
            assert submitted["ok"] is True
            task_id = submitted["result"]["task_id"]

            def stream_initialized() -> bool:
                record = service.store.get_task(task_id)
                kinds = [event.event_type for event in service.store.list_events(task_id)]
                return (
                    record.status == "running"
                    and record.native_session_id == "native-partial-session"
                    and "agent.message" in kinds
                )

            assert wait_until(stream_initialized, timeout_seconds=15)
            before = call(
                pipe,
                "core-a",
                "task.get",
                {"workdir": "repo", "task_id": task_id},
            )
            assert before["ok"] is True
            assert before["result"]["status"] == "running"
            for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
                forbidden = call(pipe, "core-b", method, {"workdir": "repo", "task_id": task_id})
                assert forbidden["ok"] is False
                assert forbidden["error"]["code"] == "CORE_TASK_NOT_FOUND"

            cancellation = call(
                pipe, "core-a", "task.cancel", {"workdir": "repo", "task_id": task_id}
            )
            assert cancellation["ok"] is True
            assert cancellation["result"]["status"] == "cancelled"
            assert wait_until(
                lambda: service.store.get_task(task_id).status == "cancelled",
                timeout_seconds=10,
            )
            after = service.store.get_task(task_id)
            assert after.native_session_id == "native-partial-session"
            assert len(factory.clients) == 1
            assert factory.clients[0].interrupted is True
            types = [event.event_type for event in service.store.list_events(task_id)]
            assert "task.started" in types
            assert "agent.message" in types
            assert "task.cancelled" in types
            assert "task.completed" not in types
