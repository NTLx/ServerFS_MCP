from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import unix_serve

from serverfs_agent_bridge.adapters.codex_transport import CodexConnection


@pytest.mark.asyncio
async def test_codex_transport_initializes_and_routes_rpc(tmp_path: Path) -> None:
    socket_path = tmp_path / "codex.sock"
    received_response: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        initialize = json.loads(await ws.recv())
        assert initialize["method"] == "initialize"
        assert initialize["params"]["capabilities"]["experimentalApi"] is True
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "userAgent": "codex-app-server/0.153.4",
                        "codexHome": "/home/test/.codex",
                    },
                }
            )
        )

        initialized = json.loads(await ws.recv())
        assert initialized["method"] == "initialized"

        request = json.loads(await ws.recv())
        assert request["method"] == "thread/read"
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"thread": {"id": "thread-1"}},
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "text": "hello"}},
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "native-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "echo hi"},
                }
            )
        )
        received_response.set_result(json.loads(await ws.recv()))
        await ws.wait_closed()

    server = await unix_serve(handler, path=str(socket_path))
    connection = CodexConnection(
        socket_path=socket_path,
        client_name="test-client",
        client_version="test",
        request_timeout=2,
    )
    try:
        await connection.connect()
        assert connection.server_version == "0.153.4"
        result = await connection.request("thread/read", {"threadId": "thread-1"})
        assert result["thread"]["id"] == "thread-1"

        notification = await connection.next_event(timeout=1)
        assert notification["method"] == "item/completed"
        request = await connection.next_event(timeout=1)
        assert request["method"] == "item/commandExecution/requestApproval"

        await connection.respond(request["id"], {"decision": "decline"})
        response = await asyncio.wait_for(received_response, timeout=1)
        assert response == {
            "jsonrpc": "2.0",
            "id": "native-1",
            "result": {"decision": "decline"},
        }
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()
