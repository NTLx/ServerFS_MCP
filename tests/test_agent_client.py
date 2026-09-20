"""Agent Bridge UDS client protocol tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from serverfs_mcp.agent_client import (
    PROTOCOL_VERSION,
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
)


def test_agent_bridge_client_round_trip(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "bridge.sock"
        seen: dict = {}

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request = json.loads(await reader.readline())
            seen.update(request)
            response = {
                "request_id": request["request_id"],
                "ok": True,
                "result": {"task_id": "agt_1", "status": "queued"},
            }
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            client = AgentBridgeClient(socket_path, timeout_seconds=1)
            result = await client.call("task.submit", {"runtime": "codex"})
        finally:
            server.close()
            await server.wait_closed()

        assert result == {"task_id": "agt_1", "status": "queued"}
        assert seen["protocol_version"] == PROTOCOL_VERSION
        assert seen["method"] == "task.submit"
        assert seen["params"] == {"runtime": "codex"}
        assert isinstance(seen["request_id"], str) and seen["request_id"].startswith("mcp_")

    asyncio.run(scenario())


def test_agent_bridge_remote_error_preserves_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "bridge.sock"

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request = json.loads(await reader.readline())
            response = {
                "request_id": request["request_id"],
                "ok": False,
                "error": {"code": "WORKDIR_BUSY", "message": "workdir is busy"},
            }
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            client = AgentBridgeClient(socket_path, timeout_seconds=1)
            with pytest.raises(AgentBridgeRemoteError) as exc:
                await client.call("task.get", {"task_id": "agt_1"})
        finally:
            server.close()
            await server.wait_closed()

        assert exc.value.code == "WORKDIR_BUSY"
        assert exc.value.message == "workdir is busy"

    asyncio.run(scenario())


def test_agent_bridge_rejects_response_id_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "bridge.sock"

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(
                json.dumps(
                    {
                        "request_id": "wrong",
                        "ok": True,
                        "result": {},
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=str(socket_path))
        try:
            client = AgentBridgeClient(socket_path, timeout_seconds=1)
            with pytest.raises(AgentBridgeClientError):
                await client.call("runtime.list", {})
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_agent_bridge_missing_socket_is_unavailable(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = AgentBridgeClient(tmp_path / "missing.sock", timeout_seconds=0.1)
        with pytest.raises(AgentBridgeUnavailable):
            await client.call("runtime.list", {})

    asyncio.run(scenario())
