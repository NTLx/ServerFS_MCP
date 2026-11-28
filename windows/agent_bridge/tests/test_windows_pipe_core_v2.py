"""Real Windows Named Pipe Bridge v2 two-Core authorization acceptance.

Isolated random pipe + temporary SQLite/lease homes. No Task Scheduler,
production Bridge/Core, OpenAI/Cloudflare tunnel, network or external Agent.
Only the in-process fake runtime is permitted.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from pipe_support import PipeClient, RunningBridge, make_service, new_pipe_name
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
    sign_core_request,
)

require_windows_kernel("v2 Named Pipe acceptance requires the actual Windows kernel")


def _request(core_id, key_id, secret, method, params):
    envelope = {
        "protocol_version": 2,
        "bridge_id": "bridge-v2-smoke",
        "core_id": core_id,
        "key_id": key_id,
        "request_id": "v2_" + secrets.token_hex(8),
        "method": method,
        "params": params,
        "policy_revision": _POLICY,
        "issued_at": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    return {**envelope, "signature": sign_core_request(envelope, secret)}


def _call(client, core_id, key_id, secret, method, params):
    document = _request(core_id, key_id, secret, method, params)
    client.write(json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n")
    response = client.read_frame()
    assert response["request_id"] == document["request_id"]
    return response


def test_two_distinct_cores_share_one_v2_pipe_but_cannot_read_each_others_task(tmp_path: Path):
    global _POLICY
    service = make_service(tmp_path, workspace_write=True)
    _POLICY = core_workdir_policy_digest(service.policies.get("repo"))
    alpha_secret = b"A" * 32
    beta_secret = b"B" * 32
    scope = (("repo", _POLICY),)
    methods = frozenset(
        {"bridge.ping", "runtime.list", "runtime.models", "task.submit", "task.get", "task.list"}
    )
    verifier = CoreGrantVerifier(
        "bridge-v2-smoke",
        (
            CoreGrant("core-a", "key-a", alpha_secret, scope, methods),
            CoreGrant("core-b", "key-b", beta_secret, scope, methods),
        ),
        nonce_claim=service.store.claim_core_nonce,
    )
    name = new_pipe_name()
    with RunningBridge(service, pipe_name=name, core_grants=verifier) as bridge:
        with PipeClient(bridge.pipe_name) as client:
            assert client.server_pid() > 0
            assert client.request("bridge.ping", {})["error"]["code"] == "CORE_NOT_AUTHORIZED"
            ok = _call(client, "core-a", "key-a", alpha_secret, "bridge.ping", {"workdir": "repo"})
            assert ok["ok"] is True and ok["result"] == {"status": "ok"}
            catalog = _call(
                client, "core-a", "key-a", alpha_secret, "runtime.list", {"workdir": "repo"}
            )
            assert catalog["ok"] is True
            assert [item["name"] for item in catalog["result"]["runtimes"]] == ["fake"]
            submit = _call(
                client,
                "core-a",
                "key-a",
                alpha_secret,
                "task.submit",
                {
                    "runtime": "fake",
                    "workdir": "repo",
                    "path": "",
                    "profile": "workspace-write",
                    "prompt": "just a deterministic fixture",
                },
            )
            assert submit["ok"] is True, submit
            # A new pipe client has no submission response or task ID.
            with PipeClient(bridge.pipe_name) as resumed:
                discovered = _call(
                    resumed,
                    "core-a",
                    "key-a",
                    alpha_secret,
                    "task.list",
                    {"workdir": "repo", "status": "all"},
                )
                assert discovered["ok"], discovered
                tasks = discovered["result"]["tasks"]
                assert len(tasks) == 1
                task_id = tasks[0]["task_id"]
                assert task_id == submit["result"]["task_id"]
                assert tasks[0]["correlation_id"] is None
                foreign_list = _call(
                    resumed,
                    "core-b",
                    "key-b",
                    beta_secret,
                    "task.list",
                    {"workdir": "repo", "status": "all"},
                )
                assert foreign_list["ok"], foreign_list
                assert foreign_list["result"] == {"tasks": [], "next_cursor": None}
            foreign = _call(
                client,
                "core-b",
                "key-b",
                beta_secret,
                "task.get",
                {"workdir": "repo", "task_id": task_id},
            )
            assert foreign["ok"] is False
            assert foreign["error"]["code"] == "CORE_TASK_NOT_FOUND"
            owned = _call(
                client,
                "core-a",
                "key-a",
                alpha_secret,
                "task.get",
                {"workdir": "repo", "task_id": task_id},
            )
            assert owned["ok"] is True
            assert owned["result"]["task_id"] == task_id
            unauthorized = _call(
                client,
                "core-a",
                "key-a",
                beta_secret,
                "task.get",
                {"workdir": "repo", "task_id": task_id},
            )
            assert unauthorized["error"]["code"] == "CORE_NOT_AUTHORIZED"
