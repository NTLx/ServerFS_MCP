"""Disposable macOS Shared Core acceptance worker; use the Bridge's own venv."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import sys
from pathlib import Path

from serverfs_agent_bridge.adapters import claude as sdk
from serverfs_agent_bridge.adapters.claude import ClaudeAdapter
from serverfs_agent_bridge.config import ClaudeSettings
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

_SAFE_SUBTYPES = frozenset(
    {"success", "error_max_turns", "error_during_execution", "error_max_budget_usd"}
)


def write_metadata(path: Path, value: list[dict]) -> None:
    pending = path.with_suffix(".pending")
    with pending.open("x", encoding="utf-8") as stream:
        os.chmod(pending, 0o600)
        json.dump(value, stream)
    pending.replace(path)


def fingerprint(text: str) -> dict:
    encoded = text.encode("utf-8")
    return {"size_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


class TerminalMetadataClient:
    """Observe the actual SDK stream without retaining content or changing messages."""

    def __init__(self, client, path: Path) -> None:
        self._client = client
        self._path = path
        self._assistant_error = False
        self._latest_text = ""

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def receive_response(self):
        async for message in self._client.receive_response():
            if isinstance(message, sdk.AssistantMessage):
                self._assistant_error |= message.error is not None
                for block in message.content:
                    if isinstance(block, sdk.TextBlock):
                        self._latest_text = block.text
            if isinstance(message, sdk.ResultMessage):
                direct = isinstance(message.result, str)
                selected = message.result if direct else self._latest_text
                write_metadata(
                    self._path,
                    [
                        {"assistant_error": self._assistant_error},
                        {
                            "result_is_error": bool(message.is_error),
                            "selected_source": "result" if direct else "assistant_text",
                            "selected": fingerprint(selected),
                            "assistant_text": fingerprint(self._latest_text),
                            "result_subtype": (
                                message.subtype if message.subtype in _SAFE_SUBTYPES else "other"
                            ),
                        },
                    ],
                )
            yield message


async def self_test(root: Path) -> None:
    """Exercise the actual wrapper with synthetic SDK messages; no provider call."""
    sentinel = "private raw provider text"
    original = [
        sdk.AssistantMessage([], "model", error=sentinel),
        sdk.ResultMessage(
            subtype=sentinel,
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id=sentinel,
            result=sentinel,
        ),
    ]

    class FakeClient:
        async def receive_response(self):
            for message in original:
                yield message

    path = root / "metadata.json"
    wrapper = TerminalMetadataClient(FakeClient(), path)
    received = [message async for message in wrapper.receive_response()]
    assert all(a is b for a, b in zip(received, original, strict=True))
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"assistant_error": True},
        {
            "result_is_error": True,
            "result_subtype": "other",
            "selected_source": "result",
            "selected": fingerprint(sentinel),
            "assistant_text": fingerprint(""),
        },
    ]
    assert sentinel not in path.read_text(encoding="utf-8")
    original[:] = [
        sdk.AssistantMessage([sdk.TextBlock(sentinel)], "model"),
        sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="synthetic",
            result=None,
        ),
    ]
    wrapper = TerminalMetadataClient(FakeClient(), path)
    received = [message async for message in wrapper.receive_response()]
    assert all(a is b for a, b in zip(received, original, strict=True))
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"assistant_error": False},
        {
            "result_is_error": False,
            "result_subtype": "success",
            "selected_source": "assistant_text",
            "selected": fingerprint(sentinel),
            "assistant_text": fingerprint(sentinel),
        },
    ]
    assert sentinel not in path.read_text(encoding="utf-8")
    print("SDK_METADATA_SELF_TEST_PASS", flush=True)


class SyntheticClaudeClient:
    """Strict fixture SDK replacement: no subprocess, network or provider state."""

    def __init__(self, options, root: Path, expected: dict) -> None:
        cwd = Path(options.cwd).resolve()
        assert cwd.parent == root and cwd.name in expected
        assert options.resume is None and options.model is None
        self._expected = expected[cwd.name]
        self._prompt = None
        self._alias = cwd.name

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        assert fingerprint(prompt) == self._expected["prompt"]
        marker = self._expected["marker"]
        assert re.fullmatch(r"SF_SHARED_[0-9a-f]{24}", marker)
        assert marker in prompt
        self._prompt = marker

    async def receive_response(self):
        assert self._prompt is not None
        yield sdk.AssistantMessage([sdk.TextBlock(self._prompt)], "synthetic")
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="synthetic-" + self._alias,
            result=self._prompt,
        )


async def serve(root: Path, *, synthetic: bool = False) -> None:
    document = json.loads((root / "owner-input.json").read_text(encoding="utf-8"))
    assert document.get("synthetic", False) is synthetic
    expected = document.get("expected", {})
    if synthetic:
        assert set(expected) == set(document["aliases"])
    policies = [
        WorkdirAgentPolicy(
            slot=None,
            alias=alias,
            host_path=root / alias,
            mode=AgentMode.WORKSPACE_WRITE,
            runtimes=frozenset({"claude"}),
            read_only=False,
        )
        for alias in document["aliases"]
    ]
    registry = PolicyRegistry(policies)
    digests = {policy.alias: core_workdir_policy_digest(policy) for policy in policies}
    assert digests == document["digests"]
    grants = (
        CoreGrant(
            document["core_id"],
            document["key_id"],
            bytes.fromhex(document["secret_hex"]),
            tuple(digests.items()),
            frozenset({"bridge.ping", "task.submit", "task.get", "task.list", "task.events"}),
        ),
        CoreGrant(
            document["foreign_id"],
            document["foreign_key_id"],
            bytes.fromhex(document["foreign_secret_hex"]),
            ((document["aliases"][0], digests[document["aliases"][0]]),),
            frozenset({"bridge.ping", "task.get"}),
        ),
    )

    def client_factory(options):
        client = (
            SyntheticClaudeClient(options, root, expected)
            if synthetic
            else sdk.ClaudeSDKClient(options)
        )
        return TerminalMetadataClient(client, root / "metadata.json")

    store = TaskStore(root / "bridge-state")
    service = BridgeService(
        store=store,
        policies=registry,
        adapters={
            "claude": ClaudeAdapter(
                ClaudeSettings(enabled=True),
                client_factory=client_factory,
            )
        },
        lease_manager=LeaseManager(root / "locks", lease_ids=registry.lease_ids()),
    )
    server = BridgeProtocolServer(
        service=service,
        socket_path=root / "bridge.sock",
        allowed_peer_uid=os.getuid(),
        v2_only=True,
        core_grant_verifier=CoreGrantVerifier(
            document["bridge_id"], grants, nonce_claim=store.claim_core_nonce
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
    if sys.platform != "darwin" or len(sys.argv) != 3:
        raise SystemExit("disposable Darwin worker requires mode and scratch directory")
    mode, directory = sys.argv[1:]
    scratch = Path(directory).resolve(strict=True)
    if mode == "--self-test":
        asyncio.run(self_test(scratch))
    elif mode == "--serve-synthetic" and os.environ.get("SERVERFS_SHARED_CLAUDE_SYNTHETIC") == "1":
        asyncio.run(serve(scratch, synthetic=True))
    elif (
        mode == "--serve"
        and os.environ.get("SERVERFS_ENABLE_SHARED_CLAUDE_E2E") == "1"
        and os.environ.get("SERVERFS_SHARED_CLAUDE_SYNTHETIC") != "1"
    ):
        asyncio.run(serve(scratch))
    else:
        raise SystemExit("explicit acceptance opt-in required")
