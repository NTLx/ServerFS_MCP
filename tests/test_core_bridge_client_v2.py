from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from serverfs_mcp import core_bridge_client_v2 as client_module
from serverfs_mcp.core_bridge_client_v2 import (
    CoreBridgeClientV2,
    CoreBridgeClientV2Error,
    CoreBridgeRemoteError,
    CoreBridgeUnavailable,
)

AGENT_BRIDGE_SRC = Path(__file__).parents[1] / "agent_bridge" / "src"
sys.path.insert(0, str(AGENT_BRIDGE_SRC))
from serverfs_agent_bridge.core_grants import sign_core_request  # noqa: E402


def _client(path: Path, **overrides: Any) -> CoreBridgeClientV2:
    options: dict[str, Any] = {
        "bridge_id": "shared-one",
        "core_id": "core-blue",
        "key_id": "key-blue",
        "secret": bytes(range(32)),
        "workdir_alias": "Repo",
        "policy_revision": "a" * 64,
        "timeout_seconds": 1,
    }
    options.update(overrides)
    return CoreBridgeClientV2(path, **options)


def test_canonical_signature_matches_v2_verifier_and_frozen_vector() -> None:
    secret = bytes(range(32))
    envelope = {
        "protocol_version": 2,
        "bridge_id": "shared-one",
        "core_id": "core-blue",
        "key_id": "grant-blue-1",
        "request_id": "req-1",
        "method": "bridge.ping",
        "params": {},
        "policy_revision": "a" * 64,
        "issued_at": 1794672000,
        "nonce": bytes(range(16)).hex(),
    }
    expected = hmac.new(
        secret,
        json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert (
        client_module._canonical_message(envelope)
        == json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )
    assert sign_core_request(envelope, secret) == expected
    assert expected == "bb0fcd07622dc9f7ac10c6fc90b7abdd1d07caf1c0a49390d39c8d54ebf54b0f"


def test_client_validates_credentials_and_never_reprs_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(client_module.sys, "platform", "darwin")
    secret = b"s" * 32
    client = _client(tmp_path / "bridge.sock", secret=secret)
    assert secret.decode() not in repr(client)
    with pytest.raises(ValueError, match="32 bytes"):
        _client(tmp_path / "bridge.sock", secret=b"short")
    with pytest.raises(ValueError, match="identity"):
        _client(tmp_path / "bridge.sock", core_id="../x")


def test_signed_unix_exchange_and_mismatched_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module.sys, "platform", "darwin")
    seen: list[dict[str, Any]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        seen.append(request)
        signature = request.pop("signature")
        assert signature == sign_core_request(request, b"k" * 32)
        request_id = request["request_id"]
        if len(seen) == 1:
            response = {"request_id": request_id, "ok": True, "result": {"pong": True}}
        else:
            response = {"request_id": "wrong", "ok": True, "result": {}}
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run() -> None:
        server = await asyncio.start_unix_server(serve, path=str(path))
        try:
            client = _client(path, secret=b"k" * 32)
            assert await client.call("bridge.ping", {}) == {"pong": True}
            request = seen[0]
            assert request["protocol_version"] == 2
            assert request["params"] == {"workdir": "Repo"}
            assert len(request["nonce"]) == 32
            assert isinstance(request["issued_at"], int)
            with pytest.raises(CoreBridgeClientV2Error, match="invalid response"):
                await client.call("bridge.ping", {})
        finally:
            server.close()
            await server.wait_closed()
            path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="cb2-", dir="/tmp") as socket_dir:
        path = Path(socket_dir) / "bridge.sock"
        asyncio.run(run())


def test_timeout_remote_error_and_invalid_calls_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module.sys, "platform", "darwin")

    async def slow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        await asyncio.sleep(0.2)
        writer.close()

    async def run() -> None:
        server = await asyncio.start_unix_server(slow, path=str(path))
        try:
            with pytest.raises(CoreBridgeUnavailable, match="timed out"):
                await _client(path, timeout_seconds=0.01).call("bridge.ping", {})
        finally:
            server.close()
            await server.wait_closed()
            path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="cb2-", dir="/tmp") as socket_dir:
        path = Path(socket_dir) / "bridge.sock"
        asyncio.run(run())
        with pytest.raises(ValueError, match="method"):
            asyncio.run(_client(path).call("shell.exec", {}))
        with pytest.raises(ValueError, match="params"):
            asyncio.run(_client(path).call("bridge.ping", {"workdir": "Elsewhere"}))
    with pytest.raises(CoreBridgeRemoteError, match="NOPE"):
        client_module._parse_response(
            b'{"request_id":"r","ok":false,"error":{"code":"NOPE","message":"denied"}}', "r"
        )


def test_non_darwin_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(client_module.sys, "platform", "linux")
    with pytest.raises(ValueError, match="only on macOS"):
        _client(tmp_path / "bridge.sock")
