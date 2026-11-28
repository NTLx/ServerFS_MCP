"""Real Windows pipe: two Core owners, durable replay and owner-side key rotation.

All service state, lease files, tasks and workdirs are pytest temporary data.
Only FakeAdapter is installed. This never connects to production ServerFS,
Windows Task Scheduler, any public Connector or native Codex/Claude provider.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from pipe_support import PipeClient, RunningBridge, make_service, new_pipe_name
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.core_grants import (
    core_workdir_policy_digest,
    sign_core_request,
)
from serverfs_agent_bridge.core_grants_private import (
    load_private_core_grants,
    provision_private_core_grants,
)

require_windows_kernel("native Windows Named Pipe and Bridge state required")

_BRIDGE = "v2-rotation-smoke"
_METHODS = [
    "bridge.ping",
    "runtime.list",
    "task.submit",
    "task.get",
    "task.events",
    "task.result.read",
    "task.cancel",
]


def _private_document(digest: str, credentials: dict[tuple[str, str], bytes]) -> dict:
    return {
        "schema_version": 1,
        "bridge_id": _BRIDGE,
        "grants": [
            {
                "core_id": core,
                "key_id": key_id,
                "secret_hex": secret.hex(),
                "methods": list(_METHODS),
                "workdirs": [{"alias": "repo", "policy_sha256": digest}],
            }
            for (core, key_id), secret in credentials.items()
        ],
    }


def _signed(
    *,
    core: str,
    key_id: str,
    secret: bytes,
    digest: str,
    method: str,
    params: dict,
    nonce: str | None = None,
) -> bytes:
    unsigned = {
        "protocol_version": 2,
        "bridge_id": _BRIDGE,
        "core_id": core,
        "key_id": key_id,
        "request_id": "req-" + secrets.token_hex(8),
        "method": method,
        "params": params,
        "policy_revision": digest,
        "issued_at": int(time.time()),
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }
    document = {**unsigned, "signature": sign_core_request(unsigned, secret)}
    return json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"


def _exchange(pipe: PipeClient, request: bytes) -> dict:
    pipe.write(request)
    return pipe.read_frame()


def _expect_error(pipe: PipeClient, request: bytes, code: str) -> None:
    reply = _exchange(pipe, request)
    assert reply["ok"] is False
    assert reply["error"]["code"] == code


def _request(
    core: str,
    key_id: str,
    secret: bytes,
    digest: str,
    method: str,
    params: dict,
    *,
    nonce: str | None = None,
) -> bytes:
    return _signed(
        core=core,
        key_id=key_id,
        secret=secret,
        digest=digest,
        method=method,
        params=params,
        nonce=nonce,
    )


def _provision(service, digest: str, credentials: dict[tuple[str, str], bytes]) -> None:
    provision_private_core_grants(
        store=service.store,
        bridge_id=_BRIDGE,
        policies=service.policies,
        document=_private_document(digest, credentials),
    )


def _running(service):
    verifier = load_private_core_grants(
        store=service.store, bridge_id=_BRIDGE, policies=service.policies
    )
    assert verifier.durable_replay_protection
    return RunningBridge(service, pipe_name=new_pipe_name(), core_grants=verifier)


def test_real_pipe_core_scoped_cancellation_is_durable(tmp_path: Path) -> None:
    """Owner cancellation cannot terminate a peer Core's independent task."""
    service = make_service(tmp_path, workspace_write=True)
    digest = core_workdir_policy_digest(service.policies.get("repo"))
    a_key, b_key = secrets.token_bytes(32), secrets.token_bytes(32)
    credentials = {("core-a", "key-a"): a_key, ("core-b", "key-b"): b_key}
    _provision(service, digest, credentials)

    def call(pipe: PipeClient, core: str, method: str, params: dict) -> dict:
        key_id, key = ("key-a", a_key) if core == "core-a" else ("key-b", b_key)
        return _exchange(pipe, _request(core, key_id, key, digest, method, params))

    def wait_status(pipe: PipeClient, core: str, task_id: str, expected: str) -> dict:
        deadline = time.monotonic() + 5
        while True:
            response = call(pipe, core, "task.get", {"workdir": "repo", "task_id": task_id})
            assert response["ok"] is True
            task = response["result"]
            if task["status"] == expected:
                return task
            assert time.monotonic() < deadline, (
                f"owned task did not reach expected status {expected}"
            )
            time.sleep(0.02)

    submit = {
        "runtime": "fake",
        "workdir": "repo",
        "path": "",
        # Review tasks do not take the shared workdir write lease, allowing
        # both Core owners to remain active without violating lease policy.
        "profile": "review",
        "prompt": "wait: test-only task awaiting explicit cancellation",
    }
    with _running(service) as live:
        with PipeClient(live.pipe_name) as pipe:
            a = call(pipe, "core-a", "task.submit", submit)
            b = call(pipe, "core-b", "task.submit", submit)
            assert a["ok"] is True and b["ok"] is True
            a_id, b_id = a["result"]["task_id"], b["result"]["task_id"]
            assert a_id != b_id
            wait_status(pipe, "core-a", a_id, "running")
            wait_status(pipe, "core-b", b_id, "running")

            # A validly signed foreign-Core cancellation is denied before
            # touching either task, with the same non-enumerating error.
            foreign = call(pipe, "core-b", "task.cancel", {"workdir": "repo", "task_id": a_id})
            assert foreign["ok"] is False
            assert foreign["error"]["code"] == "CORE_TASK_NOT_FOUND"
            assert (
                call(pipe, "core-a", "task.get", {"workdir": "repo", "task_id": a_id})["result"][
                    "status"
                ]
                == "running"
            )

            cancelled = call(pipe, "core-a", "task.cancel", {"workdir": "repo", "task_id": a_id})
            assert cancelled["ok"] is True
            assert cancelled["result"]["status"] == "cancelled"
            wait_status(pipe, "core-a", a_id, "cancelled")
            assert (
                call(pipe, "core-b", "task.get", {"workdir": "repo", "task_id": b_id})["result"][
                    "status"
                ]
                == "running"
            )

            cross = call(pipe, "core-a", "task.cancel", {"workdir": "repo", "task_id": b_id})
            assert cross["ok"] is False
            assert cross["error"]["code"] == "CORE_TASK_NOT_FOUND"
            assert (
                call(pipe, "core-b", "task.get", {"workdir": "repo", "task_id": b_id})["result"][
                    "status"
                ]
                == "running"
            )

            # Cancel the peer's own task before stopping the disposable pipe.
            own_b = call(pipe, "core-b", "task.cancel", {"workdir": "repo", "task_id": b_id})
            assert own_b["ok"] is True
            wait_status(pipe, "core-b", b_id, "cancelled")

    # A new Bridge generation reopens the durable private state; cancelled
    # tasks remain owned by their original Core IDs, not by a key or process.
    reopened = make_service(tmp_path, workspace_write=True)
    _provision(reopened, digest, credentials)
    with _running(reopened) as live:
        with PipeClient(live.pipe_name) as pipe:
            assert (
                call(pipe, "core-a", "task.get", {"workdir": "repo", "task_id": a_id})["result"][
                    "status"
                ]
                == "cancelled"
            )
            assert (
                call(pipe, "core-b", "task.get", {"workdir": "repo", "task_id": b_id})["result"][
                    "status"
                ]
                == "cancelled"
            )
            for core, foreign_id in (("core-a", b_id), ("core-b", a_id)):
                for method in ("task.get", "task.events", "task.result.read", "task.cancel"):
                    denied = call(pipe, core, method, {"workdir": "repo", "task_id": foreign_id})
                    assert denied["ok"] is False
                    assert denied["error"]["code"] == "CORE_TASK_NOT_FOUND"


def test_real_pipe_core_ownership_replay_and_private_key_rotation(tmp_path: Path) -> None:
    """All rotation steps occur only after the prior temporary pipe is stopped."""
    first = make_service(tmp_path, workspace_write=True)
    digest = core_workdir_policy_digest(first.policies.get("repo"))
    a_old = secrets.token_bytes(32)
    a_new = secrets.token_bytes(32)
    b_key = secrets.token_bytes(32)
    old = {("core-a", "key-old"): a_old, ("core-b", "key-b"): b_key}
    overlap = {**old, ("core-a", "key-new"): a_new}
    rotated = {("core-b", "key-b"): b_key, ("core-a", "key-new"): a_new}
    _provision(first, digest, old)

    def call(pipe, core, key_id, key, method, params, *, nonce=None):
        return _exchange(pipe, _request(core, key_id, key, digest, method, params, nonce=nonce))

    ping = {"workdir": "repo"}
    submit = {
        "runtime": "fake",
        "workdir": "repo",
        "path": "",
        "profile": "workspace-write",
        "prompt": "test-only deterministic task",
    }
    first_nonce = secrets.token_hex(16)
    signed_once = _request(
        "core-a", "key-old", a_old, digest, "bridge.ping", ping, nonce=first_nonce
    )

    # Stage A: both Cores are independently valid, but task IDs are Core-owned.
    with _running(first) as live:
        with PipeClient(live.pipe_name) as pipe:
            assert _exchange(pipe, signed_once)["result"] == {"status": "ok"}
            _expect_error(pipe, signed_once, "CORE_NOT_AUTHORIZED")
            a = call(pipe, "core-a", "key-old", a_old, "task.submit", submit)
            b = call(pipe, "core-b", "key-b", b_key, "task.submit", submit)
            assert a["ok"] is True and b["ok"] is True
            a_id, b_id = a["result"]["task_id"], b["result"]["task_id"]
            assert a_id != b_id

            own_a = call(
                pipe,
                "core-a",
                "key-old",
                a_old,
                "task.get",
                {"workdir": "repo", "task_id": a_id},
            )
            own_b = call(
                pipe,
                "core-b",
                "key-b",
                b_key,
                "task.get",
                {"workdir": "repo", "task_id": b_id},
            )
            assert own_a["ok"] is True and own_b["ok"] is True
            assert own_a["result"]["task_id"] == a_id
            assert own_b["result"]["task_id"] == b_id

            for method, params in (
                ("task.get", {}),
                ("task.events", {}),
                ("task.result.read", {}),
                ("task.cancel", {}),
            ):
                _expect_error(
                    pipe,
                    _request(
                        "core-b",
                        "key-b",
                        b_key,
                        digest,
                        method,
                        {"workdir": "repo", "task_id": a_id, **params},
                    ),
                    "CORE_TASK_NOT_FOUND",
                )
            # The indistinguishable error denies unknown and foreign tasks.
            _expect_error(
                pipe,
                _request(
                    "core-b",
                    "key-b",
                    b_key,
                    digest,
                    "task.get",
                    {"workdir": "repo", "task_id": "agt_nonexistent"},
                ),
                "CORE_TASK_NOT_FOUND",
            )

    # Stage B: on a new real pipe and reopened durable SQLite state the exact
    # old signed nonce is still rejected. Both old and new A keys can overlap.
    second = make_service(tmp_path, workspace_write=True)
    _provision(second, digest, overlap)
    with _running(second) as live:
        with PipeClient(live.pipe_name) as pipe:
            _expect_error(pipe, signed_once, "CORE_NOT_AUTHORIZED")
            assert call(pipe, "core-a", "key-old", a_old, "bridge.ping", ping)["ok"]
            assert call(pipe, "core-a", "key-new", a_new, "bridge.ping", ping)["ok"]
            assert call(pipe, "core-b", "key-b", b_key, "bridge.ping", ping)["ok"]
            # Task ownership in the reopened state remains Core-specific.
            _expect_error(
                pipe,
                _request(
                    "core-b",
                    "key-b",
                    b_key,
                    digest,
                    "task.get",
                    {"workdir": "repo", "task_id": a_id},
                ),
                "CORE_TASK_NOT_FOUND",
            )
            owned = call(
                pipe,
                "core-a",
                "key-new",
                a_new,
                "task.get",
                {"workdir": "repo", "task_id": a_id},
            )
            assert owned["ok"] is True and owned["result"]["task_id"] == a_id

    # Stage C: owner-persisted revoke of the old key, then new verifier/pipeline.
    # Old Core A key cannot return even if it signs a brand-new nonce.
    third = make_service(tmp_path, workspace_write=True)
    _provision(third, digest, rotated)
    with _running(third) as live:
        with PipeClient(live.pipe_name) as pipe:
            _expect_error(
                pipe,
                _request("core-a", "key-old", a_old, digest, "bridge.ping", ping),
                "CORE_NOT_AUTHORIZED",
            )
            assert call(pipe, "core-a", "key-new", a_new, "bridge.ping", ping)["ok"]
            assert call(pipe, "core-b", "key-b", b_key, "bridge.ping", ping)["ok"]
            # Revoking a signing key does not transfer ownership to another Core.
            _expect_error(
                pipe,
                _request(
                    "core-b",
                    "key-b",
                    b_key,
                    digest,
                    "task.get",
                    {"workdir": "repo", "task_id": a_id},
                ),
                "CORE_TASK_NOT_FOUND",
            )
            assert (
                call(
                    pipe,
                    "core-a",
                    "key-new",
                    a_new,
                    "task.get",
                    {"workdir": "repo", "task_id": a_id},
                )["ok"]
                is True
            )
