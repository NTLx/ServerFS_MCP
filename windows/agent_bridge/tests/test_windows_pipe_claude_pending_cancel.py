"""Native Windows signed-Pipe HITL cancellation and task-ownership regression.

Real NT Named Pipe, signed two-Core grants, SQLite private state and lock
leases. Deterministic fake Claude SDK only; no provider, model, tool, DNS,
Scheduler tasks or access to the installation's real workspace.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pytest

from pipe_support import PipeClient, RunningBridge, new_pipe_name, wait_until
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.core_grants_private import (
    load_private_core_grants,
    provision_private_core_grants,
)
from test_claude_adapter import make_service
from test_windows_pipe_core_v2_claude_adapter_ownership import BRIDGE, METHODS, _call

require_windows_kernel("requires native Windows Named Pipe/HMAC/SQLite")


@pytest.mark.parametrize(
    ("prompt", "waiting_status", "pending_kind"),
    [
        ("approval", "waiting_for_approval", "approval"),
        ("question", "waiting_for_question", "question"),
    ],
)
def test_native_pipe_cancel_claude_pending_hitl_stales_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    waiting_status: str,
    pending_kind: str,
) -> None:
    service, fake = make_service(tmp_path, monkeypatch, event_idle_timeout_seconds=0.02)
    digest = core_workdir_policy_digest(service.policies.get("repo"))
    keys = {
        "core-a": ("key-a", secrets.token_bytes(32)),
        "core-b": ("key-b", secrets.token_bytes(32)),
    }
    provision_private_core_grants(
        store=service.store,
        bridge_id=BRIDGE,
        policies=service.policies,
        document={
            "schema_version": 1,
            "bridge_id": BRIDGE,
            "grants": [
                {
                    "core_id": core,
                    "key_id": key[0],
                    "secret_hex": key[1].hex(),
                    "methods": [*METHODS, "task.approval.respond", "task.question.answer"],
                    "workdirs": [{"alias": "repo", "policy_sha256": digest}],
                }
                for core, key in keys.items()
            ],
        },
    )
    grants = load_private_core_grants(
        store=service.store, bridge_id=BRIDGE, policies=service.policies
    )
    assert grants.durable_replay_protection

    def call(pipe: PipeClient, core: str, method: str, params: dict) -> dict:
        key_id, secret = keys[core]
        return _call(pipe, core, key_id, secret, digest, method, params)

    with RunningBridge(service, pipe_name=new_pipe_name(), core_grants=grants) as bridge:
        with PipeClient(bridge.pipe_name) as pipe:
            submitted = call(
                pipe,
                "core-a",
                "task.submit",
                {
                    "runtime": "claude",
                    "workdir": "repo",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": prompt,
                },
            )
            assert submitted["ok"] is True
            task_id = submitted["result"]["task_id"]

            def has_pending() -> bool:
                row = service.store.get_task(task_id)
                return row.status == waiting_status and row.pending_request_id is not None

            assert wait_until(has_pending, timeout_seconds=15)
            pending_id = service.store.get_task(task_id).pending_request_id
            assert isinstance(pending_id, str)
            pending = service.store.get_request(pending_id)
            assert pending.kind == pending_kind
            assert pending.status == "pending"
            assert pending.payload
            own = call(pipe, "core-a", "task.get", {"workdir": "repo", "task_id": task_id})
            assert own["ok"] is True
            assert own["result"]["status"] == waiting_status
            # The fake SDK is paused on a real Bridge interaction; the Provider
            # idle clock must not expire merely because the human hasn't answered.
            time.sleep(0.07)
            assert service.store.get_task(task_id).status == waiting_status

            for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
                refused = call(pipe, "core-b", method, {"workdir": "repo", "task_id": task_id})
                assert refused["ok"] is False
                assert refused["error"]["code"] == "CORE_TASK_NOT_FOUND"

            cancelled = call(pipe, "core-a", "task.cancel", {"workdir": "repo", "task_id": task_id})
            assert cancelled["ok"] is True
            assert cancelled["result"]["status"] == "cancelled"

            def terminal_clean() -> bool:
                row = service.store.get_task(task_id)
                request = service.store.get_request(pending_id)
                return (
                    row.status == "cancelled"
                    and row.pending_request_id is None
                    and request.status == "stale"
                )

            assert wait_until(terminal_clean, timeout_seconds=10)

            # A human response to a cancelled task must fail. This checks
            # both Core ownership and stale-request rejection through the
            # real signed Windows Named Pipe, not a mock RPC.
            method = "task.approval.respond"
            if pending_kind == "question":
                method = "task.question.answer"
            response = {"workdir": "repo", "task_id": task_id, "request_id": pending_id}
            if pending_kind == "approval":
                response["decision"] = "approve_once"
            else:
                response["answers"] = [{"question_id": "q0", "selected_option_ids": ["B"]}]
            foreign = call(pipe, "core-b", method, response)
            assert foreign["ok"] is False
            assert foreign["error"]["code"] == "CORE_TASK_NOT_FOUND"
            late = call(pipe, "core-a", method, response)
            assert late["ok"] is False
            assert late["error"]["code"] == "REQUEST_STALE"

            assert len(fake.clients) == 1
            assert fake.clients[0].interrupted is True
            events = [item.event_type for item in service.store.list_events(task_id)]
            assert "approval.resolved" not in events
            assert "question.answered" not in events
            assert "task.started" in events
            assert "task.cancelled" in events
            assert "task.completed" not in events


def test_native_pipe_claude_sdk_delta_then_owner_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real IPC persistence of a pinned-SDK StreamEvent before owner cancellation."""
    import asyncio

    from claude_agent_sdk import StreamEvent

    import serverfs_agent_bridge.adapters.claude as claude_module
    from test_claude_adapter import FakeClaudeClient, FakeResultMessage

    service, factory = make_service(tmp_path, monkeypatch)

    class DeltaThenHoldClient(FakeClaudeClient):
        async def receive_response(self):
            assert self.prompt == "native-sdk-delta-hold"
            yield claude_module.SystemMessage(
                subtype="init", data={"session_id": "native-sdk-stream-session"}
            )
            yield StreamEvent(
                uuid="native-stream-message",
                session_id="native-sdk-stream-session",
                parent_tool_use_id=None,
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "stream-before-cancel"},
                },
            )
            while not self.interrupted:
                await asyncio.sleep(0.01)
            yield FakeResultMessage(
                result="interrupted",
                session_id="native-sdk-stream-session",
                terminal_reason="aborted_streaming",
            )

    def client_factory(options):
        client = DeltaThenHoldClient(options)
        factory.clients.append(client)
        return client

    service.adapters["claude"]._client_factory = client_factory
    digest = core_workdir_policy_digest(service.policies.get("repo"))
    keys = {
        "core-a": ("key-a", secrets.token_bytes(32)),
        "core-b": ("key-b", secrets.token_bytes(32)),
    }
    provision_private_core_grants(
        store=service.store,
        bridge_id=BRIDGE,
        policies=service.policies,
        document={
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
        },
    )
    grants = load_private_core_grants(
        store=service.store, bridge_id=BRIDGE, policies=service.policies
    )

    def call(pipe: PipeClient, core: str, method: str, params: dict) -> dict:
        key_id, secret = keys[core]
        return _call(pipe, core, key_id, secret, digest, method, params)

    with RunningBridge(service, pipe_name=new_pipe_name(), core_grants=grants) as bridge:
        with PipeClient(bridge.pipe_name) as pipe:
            submitted = call(
                pipe,
                "core-a",
                "task.submit",
                {
                    "runtime": "claude",
                    "workdir": "repo",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": "native-sdk-delta-hold",
                },
            )
            assert submitted["ok"] is True
            task_id = submitted["result"]["task_id"]

            def streamed_and_running() -> bool:
                record = service.store.get_task(task_id)
                kinds = [event.event_type for event in service.store.list_events(task_id)]
                return (
                    record.status == "running"
                    and record.native_session_id == "native-sdk-stream-session"
                    and "agent.text_delta" in kinds
                )

            assert wait_until(streamed_and_running, timeout_seconds=15)
            assert factory.clients[0].options.include_partial_messages is True
            page = call(
                pipe,
                "core-a",
                "task.events",
                {"workdir": "repo", "task_id": task_id, "after_event_id": 0, "limit": 30},
            )
            assert page["ok"] is True
            fragments = [
                event["payload"]
                for event in page["result"]["events"]
                if event["event_type"] == "agent.text_delta"
            ]
            assert fragments == [{"size_bytes": len(b"stream-before-cancel"), "index": 0}]

            for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
                rejected = call(pipe, "core-b", method, {"workdir": "repo", "task_id": task_id})
                assert rejected["ok"] is False
                assert rejected["error"]["code"] == "CORE_TASK_NOT_FOUND"

            cancelled = call(pipe, "core-a", "task.cancel", {"workdir": "repo", "task_id": task_id})
            assert cancelled["ok"] is True
            assert cancelled["result"]["status"] == "cancelled"
            assert wait_until(
                lambda: service.store.get_task(task_id).status == "cancelled",
                timeout_seconds=10,
            )
            assert factory.clients[0].interrupted is True
            event_types = [e.event_type for e in service.store.list_events(task_id)]
            assert "agent.text_delta" in event_types
            assert "task.cancelled" in event_types
            assert "task.completed" not in event_types
