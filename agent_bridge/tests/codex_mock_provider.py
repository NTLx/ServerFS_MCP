"""A transport-neutral Codex App Server double, shared by the UDS and loopback-WS suites.

The provider semantics under test -- model discovery, thread start/resume, turns, steer, interrupt,
approvals, questions, events, results, reconciliation and request-scoped model override -- are one
contract, not two. Only the socket underneath differs: Linux reaches the managed daemon over an
AF_UNIX control socket, Windows reaches a Bridge-owned child over an authenticated loopback
WebSocket. So the protocol behaviour lives here once and each suite supplies only a way in.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve, unix_serve

from platform_contract import WINDOWS, require_linux_kernel
from serverfs_agent_bridge.adapters.codex_transport import (
    CodexEndpoint,
    LoopbackWebSocketEndpoint,
    UnixSocketEndpoint,
)

#: The loopback double authenticates exactly as the real listener does, so a client that forgot its
#: bearer is refused here rather than passing a test it would fail against the provider.
LOOPBACK_TOKEN = "serverfs-test-capability-token"


class MockCodexServer:
    """The JSON-RPC behaviour of a Codex App Server, with no transport of its own."""

    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home
        self.server = None
        self.socket_path: Path | None = None
        self._endpoint: CodexEndpoint | None = None
        self.thread_starts = 0
        self.thread_start_params: list[dict[str, Any]] = []
        self.thread_resumes: list[str] = []
        self.thread_resume_params: list[dict[str, Any]] = []
        self.steers: list[str] = []
        self.interrupts = 0
        self.turn_starts: list[dict[str, Any]] = []
        self.native_responses: dict[str, dict[str, Any]] = {}

    async def handle(self, ws) -> None:
        initialize = json.loads(await ws.recv())
        assert initialize["method"] == "initialize"
        await send(
            ws,
            {
                "jsonrpc": "2.0",
                "id": initialize["id"],
                "result": {
                    "userAgent": "codex-app-server/0.153.4",
                    "codexHome": str(self.codex_home),
                },
            },
        )
        initialized = json.loads(await ws.recv())
        assert initialized["method"] == "initialized"

        thread_id = "thread-1"
        turn_id = "turn-1"
        while True:
            try:
                message = json.loads(await ws.recv())
            except Exception:
                return

            method = message.get("method")
            if method == "model/list":
                await respond(
                    ws,
                    message,
                    {
                        "data": [
                            {
                                "model": "gpt-5.6-codex",
                                "displayName": "GPT-5.6 Codex",
                                "description": "Coding model",
                                "isDefault": True,
                                "hidden": False,
                                "inputModalities": ["text", "image"],
                                "supportedReasoningEfforts": ["medium", "high"],
                                "defaultReasoningEffort": "medium",
                            },
                            {
                                "model": "gpt-5.6-mini",
                                "displayName": "GPT-5.6 Mini",
                                "description": "Fast coding model",
                                "isDefault": False,
                                "hidden": False,
                            },
                        ],
                        "nextCursor": None,
                    },
                )
                continue
            if method == "thread/start":
                self.thread_starts += 1
                params = message["params"]
                self.thread_start_params.append(dict(params))
                assert (
                    {
                        "cwd",
                        "serviceName",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "config",
                    }
                    <= set(params)
                    <= {
                        "cwd",
                        "serviceName",
                        "model",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "config",
                    }
                )
                assert params["approvalPolicy"] == "on-request"
                assert params["approvalsReviewer"] == "user"
                assert params["config"] == {
                    "features.request_permissions_tool": True,
                    "features.guardian_approval": True,
                }
                assert "sandbox" not in message["params"]
                await respond(ws, message, {"thread": {"id": thread_id}})
                continue
            if method == "thread/resume":
                params = message["params"]
                self.thread_resume_params.append(dict(params))
                assert (
                    {
                        "threadId",
                        "cwd",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "config",
                    }
                    <= set(params)
                    <= {
                        "threadId",
                        "cwd",
                        "model",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "config",
                    }
                )
                assert params["approvalPolicy"] == "on-request"
                assert params["approvalsReviewer"] == "user"
                assert params["config"] == {
                    "features.request_permissions_tool": True,
                    "features.guardian_approval": True,
                }
                assert "sandbox" not in message["params"]
                thread_id = message["params"]["threadId"]
                self.thread_resumes.append(thread_id)
                await respond(ws, message, {"thread": {"id": thread_id}})
                continue
            if method == "turn/start":
                params = message["params"]
                self.turn_starts.append(params)
                assert set(params) == {
                    "threadId",
                    "input",
                    "cwd",
                    "approvalPolicy",
                    "approvalsReviewer",
                }
                assert params["approvalPolicy"] == "on-request"
                assert params["approvalsReviewer"] == "user"
                assert "sandboxPolicy" not in params
                prompt = params["input"][0]["text"]
                await respond(
                    ws,
                    message,
                    {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
                )
                if prompt == "approval":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-approval",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": ["pytest", "-q"],
                                "cwd": message["params"]["cwd"],
                                "reason": "Run tests",
                                "availableDecisions": [
                                    "accept",
                                    "acceptForSession",
                                    "decline",
                                    "cancel",
                                ],
                            },
                        },
                    )
                elif prompt == "unsafe-network":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-network",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": ["curl", "https://example.com"],
                                "cwd": message["params"]["cwd"],
                                "networkApprovalContext": {
                                    "host": "example.com",
                                    "protocol": "https",
                                },
                                "availableDecisions": ["accept", "decline", "cancel"],
                            },
                        },
                    )
                elif prompt == "file-approval":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-file",
                            "method": "item/fileChange/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Apply patch",
                                "grantRoot": message["params"]["cwd"],
                            },
                        },
                    )
                elif prompt == "file-outside":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-file-outside",
                            "method": "item/fileChange/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Outside write",
                                "grantRoot": str(Path(message["params"]["cwd"]).parent),
                            },
                        },
                    )
                elif prompt == "permission":
                    cwd = Path(message["params"]["cwd"])
                    inside = str(cwd / "generated")
                    outside = str(cwd.parent / "outside-generated")
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-permission",
                            "method": "item/permissions/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "reason": "Need generated output",
                                "cwd": message["params"]["cwd"],
                                "permissions": {
                                    "fileSystem": {
                                        "read": [inside, outside],
                                        "write": [inside],
                                    }
                                },
                            },
                        },
                    )
                elif prompt == "mcp-elicitation":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-mcp-elicitation",
                            "method": "mcpServer/elicitation/request",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "serverName": "example-mcp",
                                "mode": "form",
                                "message": "Need additional input",
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                    "required": ["value"],
                                },
                            },
                        },
                    )
                elif prompt == "malformed-question":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-malformed",
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "questions": [42],
                            },
                        },
                    )
                elif prompt == "question":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-question",
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "itemId": "item-q",
                                "questions": [
                                    {
                                        "id": "q1",
                                        "header": "Choice",
                                        "question": "Which option?",
                                        "isOther": True,
                                        "isSecret": False,
                                        "options": [
                                            {"label": "A", "description": "first"},
                                            {"label": "B", "description": "second"},
                                        ],
                                    }
                                ],
                                "isBlocking": True,
                                "autoResolutionMs": None,
                            },
                        },
                    )
                elif prompt == "auto-resolve":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": "native-auto",
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": "echo wait",
                                "availableDecisions": ["accept", "decline", "cancel"],
                            },
                        },
                    )
                    await asyncio.sleep(0.05)
                    await notify_resolved(ws, thread_id, "native-auto")
                    await complete(ws, thread_id, turn_id, "auto-done")
                elif prompt == "integer-id":
                    # The official protocol types a request id as `string | int64`,
                    # so a numeric id must survive the response round trip.
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "id": 4242,
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                "threadId": thread_id,
                                "turnId": turn_id,
                                "command": "echo int",
                                "availableDecisions": ["accept", "decline"],
                            },
                        },
                    )
                elif prompt == "steer":
                    await send(
                        ws,
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/started",
                            "params": {
                                "threadId": thread_id,
                                "turn": {"id": turn_id, "status": "inProgress", "items": []},
                            },
                        },
                    )
                elif prompt == "wait":
                    pass
                else:
                    await complete(ws, thread_id, turn_id, f"done:{prompt}")
                continue
            if method == "turn/steer":
                text = message["params"]["input"][0]["text"]
                self.steers.append(text)
                await respond(ws, message, {"turnId": turn_id})
                await complete(ws, thread_id, turn_id, f"steered:{text}")
                continue
            if method == "turn/interrupt":
                self.interrupts += 1
                await respond(ws, message, {})
                await send(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "status": "interrupted",
                                "items": [],
                            },
                        },
                    },
                )
                continue

            if "id" in message and "method" not in message:
                request_id = str(message["id"])
                self.native_responses[request_id] = message
                await notify_resolved(ws, thread_id, request_id)
                if "error" in message:
                    code = message["error"].get("code")
                    await complete(ws, thread_id, turn_id, f"request-error:{code}")
                elif request_id == "native-question":
                    answer = message["result"]["answers"]["q1"]["answers"]
                    await complete(
                        ws,
                        thread_id,
                        turn_id,
                        "question:" + ",".join(answer),
                    )
                elif request_id == "native-permission":
                    permissions = message["result"].get("permissions", {})
                    scope = message["result"].get("scope", "turn")
                    await complete(
                        ws,
                        thread_id,
                        turn_id,
                        f"permission:{scope}:{bool(permissions)}",
                    )
                else:
                    decision = message["result"].get("decision", "unknown")
                    await complete(ws, thread_id, turn_id, f"approval:{decision}")

    async def start(self) -> None:
        """Serve the double over the transport this platform's adapter actually uses.

        Linux reaches the managed daemon over an AF_UNIX control socket at the layout
        ``CodexSettings.control_socket`` names. Windows reaches a Bridge-owned child over an
        authenticated loopback WebSocket, so that is what is served here -- on literal 127.0.0.1, on
        an OS-assigned port, with the capability token enforced. A client that forgot its bearer is
        refused by the double exactly as the provider refuses it, so a transport test cannot pass
        here and fail against the real listener.
        """
        if WINDOWS:
            self.server = await serve(
                self._authorized_handler(),
                host="127.0.0.1",
                port=0,
                compression=None,
            )
            port = next(iter(self.server.sockets)).getsockname()[1]
            self._endpoint = LoopbackWebSocketEndpoint(
                url=f"ws://127.0.0.1:{port}/rpc",
                token=LOOPBACK_TOKEN,
            )
            return
        require_linux_kernel("the Linux Codex transport is an AF_UNIX control socket")
        self.socket_path = self.codex_home / "app-server-control" / "app-server-control.sock"
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.server = await unix_serve(self.handle, path=str(self.socket_path))
        self._endpoint = UnixSocketEndpoint(self.socket_path)

    def _authorized_handler(self):
        async def handler(ws) -> None:
            if ws.request.headers.get("Authorization") != f"Bearer {LOOPBACK_TOKEN}":
                await ws.close(code=1008, reason="unauthorized")
                return
            await self.handle(ws)

        return handler

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    @property
    def endpoint(self) -> CodexEndpoint:
        """The endpoint this double is serving, for handing to the adapter under test."""
        if self._endpoint is None:
            raise AssertionError("MockCodexServer.start() must run before reading its endpoint")
        return self._endpoint


async def send(ws, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload))


async def respond(ws, request: dict[str, Any], result: dict[str, Any]) -> None:
    await send(ws, {"jsonrpc": "2.0", "id": request["id"], "result": result})


async def notify_resolved(ws, thread_id: str, request_id: str) -> None:
    await send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "serverRequest/resolved",
            "params": {"threadId": thread_id, "requestId": request_id},
        },
    )


async def complete(ws, thread_id: str, turn_id: str, text: str) -> None:
    await send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "text": text,
                    "phase": "final_answer",
                },
            },
        },
    )
    await send(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": [],
                },
            },
        },
    )
