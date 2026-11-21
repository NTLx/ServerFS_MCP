"""Read-only shared Bridge readiness checks."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from serverfs_mcp.core_bridge_client_v2 import (
    CoreBridgeClientV2,
    CoreBridgeRemoteError,
)
from serverfs_mcp.darwin_shared_bridge_readiness import probe_shared_v2_bridge


@pytest.fixture
def short_socket_root():
    if sys.platform != "darwin":
        pytest.skip("requires native Darwin /private/tmp and filesystem semantics")
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/private/tmp") as root:
        yield Path(root)


def _socket(root: Path, *, mode: int = 0o600) -> Path:
    path = root / "b.sock"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(path))
    finally:
        server.close()
    os.chmod(path, mode)
    return path


def _client(path: Path) -> CoreBridgeClientV2:
    client = object.__new__(CoreBridgeClientV2)
    client._socket_path = path
    return client


def test_probe_requires_exact_v2_ping_payload_and_verified_local_socket(
    short_socket_root: Path, monkeypatch
) -> None:
    path = _socket(short_socket_root)
    calls: list[tuple[str, dict[str, object]]] = []

    async def ping(self, method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"status": "ok"}

    monkeypatch.setattr(CoreBridgeClientV2, "call", ping)
    assert asyncio.run(probe_shared_v2_bridge(_client(path), path))
    assert calls == [("bridge.ping", {})]


def test_probe_rejects_untrusted_socket_paths_before_rpc(
    short_socket_root: Path, monkeypatch
) -> None:
    unsafe = _socket(short_socket_root, mode=0o660)
    other = short_socket_root / "other.sock"
    client = _client(other)
    called = False

    async def ping(self, method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"status": "ok"}

    monkeypatch.setattr(CoreBridgeClientV2, "call", ping)
    assert not asyncio.run(probe_shared_v2_bridge(client, unsafe))
    assert not called

    real_dir = short_socket_root / "r"
    real_dir.mkdir()
    target = _socket(real_dir)
    alias_dir = short_socket_root / "a"
    alias_dir.symlink_to(target.parent, target_is_directory=True)
    alias_path = alias_dir / target.name
    assert not asyncio.run(probe_shared_v2_bridge(_client(alias_path), alias_path))


def test_probe_rejects_auth_error_and_noncontract_payload(
    short_socket_root: Path, monkeypatch
) -> None:
    path = _socket(short_socket_root)

    async def denied(self, method: str, params: dict[str, object]) -> dict[str, object]:
        raise CoreBridgeRemoteError("CORE_NOT_AUTHORIZED", "denied")

    monkeypatch.setattr(CoreBridgeClientV2, "call", denied)
    assert not asyncio.run(probe_shared_v2_bridge(_client(path), path))

    async def wrong_payload(self, method: str, params: dict[str, object]) -> dict[str, object]:
        return {"status": "ok", "protocol_version": 1}

    monkeypatch.setattr(CoreBridgeClientV2, "call", wrong_payload)
    assert not asyncio.run(probe_shared_v2_bridge(_client(path), path))


def test_probe_requires_private_parent_when_requested(short_socket_root: Path, monkeypatch) -> None:
    parent = short_socket_root / "p"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    path = _socket(parent)
    assert not asyncio.run(probe_shared_v2_bridge(_client(path), path, require_private_parent=True))

    os.chmod(parent, 0o700)

    async def ping(self, method: str, params: dict[str, object]) -> dict[str, object]:
        return {"status": "ok"}

    monkeypatch.setattr(CoreBridgeClientV2, "call", ping)
    assert asyncio.run(probe_shared_v2_bridge(_client(path), path, require_private_parent=True))
