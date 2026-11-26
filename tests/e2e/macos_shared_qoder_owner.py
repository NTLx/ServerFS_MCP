"""Disposable Qoder Shared owner: fake routing or explicit real SDK diagnostic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from macos_shared_qoder_diagnostics import safe_sdk_error_class
from serverfs_agent_bridge.adapters import qoder as sdk
from serverfs_agent_bridge.adapters.base import AdapterResult
from serverfs_agent_bridge.adapters.fake import FakeAdapter
from serverfs_agent_bridge.adapters.qoder import QoderAdapter
from serverfs_agent_bridge.config import QoderSettings
from serverfs_agent_bridge.core_grants import (
    CoreGrant,
    CoreGrantVerifier,
    core_workdir_policy_digest,
)
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


class SyntheticQoderAdapter(FakeAdapter):
    def __init__(self, root: Path, expected: dict) -> None:
        super().__init__()
        self.root = root
        self.expected = expected

    @property
    def name(self) -> str:
        return "qoder"

    async def run_task(self, context):
        expected = self.expected[context.workdir]
        workdir = self.root / context.workdir
        if context.cwd != workdir or context.workdir_root != workdir:
            raise AssertionError("synthetic workdir routing mismatch")
        prompt = context.prompt.encode("utf-8")
        if expected["prompt"] != {
            "size_bytes": len(prompt),
            "sha256": hashlib.sha256(prompt).hexdigest(),
        }:
            raise AssertionError("synthetic prompt mismatch")
        marker = expected["marker"]
        if not marker or marker not in context.prompt:
            raise AssertionError("synthetic marker absent")
        await context.emit_event("turn.started", {"runtime": self.name})
        await context.emit_event("agent.message", {"text": marker})
        return AdapterResult(final_response=marker, native_session_id="synthetic-qoder")


class ObservedQoderClient:
    """Observe SDK boundaries without storing raw text, settings or exception messages."""

    def __init__(self, options, path: Path) -> None:
        self.path = path
        self.records: list[dict] = []
        self.terminal: dict = {}
        self.latest_text = ""
        self._record("client_constructing")
        try:
            self.client = sdk.QoderSDKClient(options)
        except (Exception, asyncio.CancelledError) as exc:
            self._record("client_construction_failed", exc)
            raise
        self._record("client_constructed")

    def _record(self, stage: str, error: BaseException | None = None) -> None:
        row = {"stage": stage, "monotonic_ms": time.monotonic_ns() // 1_000_000}
        if error is not None:
            row["error_class"] = safe_sdk_error_class(error)
        self.records.append(row)
        pending = self.path.with_suffix(".pending")
        with pending.open("x", encoding="utf-8") as stream:
            os.chmod(pending, 0o600)
            json.dump({"timeline": self.records, "terminal": self.terminal}, stream)
        pending.replace(self.path)

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def connect(self):
        self._record("connecting")
        try:
            await self.client.connect()
        except (Exception, asyncio.CancelledError) as exc:
            self._record("connect_failed", exc)
            raise
        self._record("connected")

    async def query(self, prompt):
        self._record("querying")
        try:
            await self.client.query(prompt)
        except (Exception, asyncio.CancelledError) as exc:
            self._record("query_failed", exc)
            raise
        self._record("query_sent")

    async def receive_response(self):
        self._record("receiving")
        try:
            async for message in self.client.receive_response():
                if isinstance(message, sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            self.latest_text = block.text
                if isinstance(message, sdk.ResultMessage):
                    selected = (
                        message.result if isinstance(message.result, str) else self.latest_text
                    ).encode("utf-8")
                    self.terminal = {
                        "is_error": bool(message.is_error),
                        "selected_bytes": len(selected),
                        "selected_sha256": hashlib.sha256(selected).hexdigest(),
                    }
                    self._record("result_received")
                yield message
        except (Exception, asyncio.CancelledError) as exc:
            self._record("receive_failed", exc)
            raise

    async def disconnect(self):
        self._record("disconnecting")
        try:
            await self.client.disconnect()
        except (Exception, asyncio.CancelledError) as exc:
            self._record("disconnect_failed", exc)
            raise
        self._record("disconnected")


async def serve(root: Path, *, synthetic: bool) -> None:
    data = json.loads((root / "owner-input.json").read_text(encoding="utf-8"))
    assert data.get("synthetic") is synthetic
    if synthetic:
        adapter = SyntheticQoderAdapter(root, data["expected"])
    else:
        adapter = QoderAdapter(
            QoderSettings(
                enabled=True,
                event_idle_timeout_seconds=45.0,
                qoder_bin=os.environ.get("SERVERFS_TEST_QODER_BUNDLED_BIN", "qodercli"),
            ),
            client_factory=lambda options: ObservedQoderClient(options, root / "qoder-sdk.json"),
        )
    policies = [
        WorkdirAgentPolicy(
            slot=None,
            alias=alias,
            host_path=root / alias,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"qoder"}),
            read_only=False,
        )
        for alias in data["aliases"]
    ]
    digests = {p.alias: core_workdir_policy_digest(p) for p in policies}
    assert digests == data["digests"]
    grants = (
        CoreGrant(
            data["core_id"],
            data["key_id"],
            bytes.fromhex(data["secret_hex"]),
            tuple(digests.items()),
            frozenset({"bridge.ping", "task.submit", "task.get", "task.list", "task.events"}),
        ),
        CoreGrant(
            data["foreign_id"],
            data["foreign_key_id"],
            bytes.fromhex(data["foreign_secret_hex"]),
            ((data["aliases"][0], digests[data["aliases"][0]]),),
            frozenset({"bridge.ping", "task.get"}),
        ),
    )
    store = TaskStore(root / "bridge-state")
    service = BridgeService(
        store=store,
        policies=PolicyRegistry(policies),
        adapters={"qoder": adapter},
        lease_manager=LeaseManager(root / "locks", lease_ids=[p.lease_id for p in policies]),
    )
    server = BridgeProtocolServer(
        service=service,
        socket_path=root / "bridge.sock",
        allowed_peer_uid=os.getuid(),
        v2_only=True,
        core_grant_verifier=CoreGrantVerifier(
            data["bridge_id"], grants, nonce_claim=store.claim_core_nonce
        ),
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)
    try:
        await server.start()
        (root / "owner-ready").touch(mode=0o600, exist_ok=False)
        await stopped.wait()
    finally:
        try:
            await service.close()
        finally:
            await server.close()


if __name__ == "__main__":
    synthetic = os.environ.get("SERVERFS_SHARED_QODER_SYNTHETIC") == "1"
    real = os.environ.get("SERVERFS_ENABLE_SHARED_QODER_E2E") == "1"
    if sys.platform != "darwin" or len(sys.argv) != 2 or synthetic == real:
        raise SystemExit("exactly one Qoder acceptance opt-in required")
    asyncio.run(serve(Path(sys.argv[1]).resolve(strict=True), synthetic=synthetic))
