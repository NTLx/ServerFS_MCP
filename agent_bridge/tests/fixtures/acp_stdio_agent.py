"""Small deterministic ACP Agent used by the stdio transport tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import acp
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SessionCapabilities,
    TextContentBlock,
    ToolCallUpdate,
)


class FixtureAgent:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.connection = None
        self.cancelled = asyncio.Event()

    def on_connect(self, connection) -> None:
        self.connection = connection

    async def initialize(self, protocol_version: int, **kwargs) -> InitializeResponse:
        if self.mode == "malformed-initialize":
            print("not-json assistant output", flush=True)
            await asyncio.Event().wait()
        if self.mode == "hang-initialize":
            await asyncio.Event().wait()
        if self.mode == "protocol-mismatch":
            return InitializeResponse(
                protocol_version=protocol_version + 1,
                agent_capabilities=AgentCapabilities(session_capabilities=SessionCapabilities()),
            )
        if self.mode == "protocol-refusal":
            raise RequestError(-32002, "Unsupported protocol version")
        if self.mode == "capabilities":
            capabilities = kwargs["client_capabilities"]
            self._record_capabilities(
                initialize=capabilities.model_dump(mode="json", by_alias=True)
            )
        if self.mode == "auth-initialize":
            raise RequestError.auth_required({"token": "fixture-secret"})
        if self.mode == "non-auth-initialize":
            raise RequestError(-32603, "Authentication required", {"token": "fixture-secret"})
        if self.mode == "exit":
            os._exit(23)
        if self.mode == "descendant":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            with open("fixture-descendant.pid", "w", encoding="ascii") as pid_file:
                pid_file.write(str(child.pid))
            print(json.dumps({"descendant_pid": child.pid}), file=sys.stderr, flush=True)
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=self.mode in {"load", "auth-load-session"},
                session_capabilities=SessionCapabilities(),
            ),
        )

    async def new_session(self, cwd: str, mcp_servers: list, **kwargs) -> NewSessionResponse:
        if self.mode == "auth-new-session":
            raise RequestError.auth_required({"token": "fixture-secret"})
        print(json.dumps({"cwd": cwd, "mcp_servers": mcp_servers}), file=sys.stderr, flush=True)
        if self.mode == "capabilities":
            self._record_capabilities(
                session={
                    "cwd": cwd,
                    "mcp_servers": mcp_servers,
                    "additional_directories": kwargs.get("additional_directories"),
                }
            )
        return NewSessionResponse(session_id="fixture-session")

    @staticmethod
    def _record_capabilities(**requests: dict) -> None:
        path = os.path.join(os.getcwd(), "acp-capabilities.json")
        try:
            with open(path, encoding="utf-8") as recorded:
                payload = json.load(recorded)
        except FileNotFoundError:
            payload = {}
        payload.update(requests)
        with open(path, "w", encoding="utf-8") as recorded:
            json.dump(payload, recorded)

    async def prompt(self, session_id: str, prompt: list, **kwargs) -> PromptResponse:
        if self.mode == "hang-prompt":
            await asyncio.Event().wait()
        if self.mode == "auth-prompt":
            raise RequestError.auth_required({"token": "fixture-secret"})
        if self.mode in {"blocked", "descendant-blocked"}:
            if self.mode == "descendant-blocked":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
                with open("fixture-descendant.pid", "w", encoding="ascii") as pid_file:
                    pid_file.write(str(child.pid))
                print(json.dumps({"descendant_pid": child.pid}), file=sys.stderr, flush=True)
            print("prompt-blocked", file=sys.stderr, flush=True)
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")
        if self.mode == "permission":
            try:
                response = await self.connection.request_permission(
                    session_id,
                    ToolCallUpdate(tool_call_id="tool-1", title="Deterministic operation"),
                    [
                        PermissionOption(
                            option_id="allow-exact", name="Allow once", kind="allow_once"
                        )
                    ],
                )
            except Exception as exc:
                print(f"permission error: {exc!r}", file=sys.stderr, flush=True)
                raise
            print(response.outcome.option_id, file=sys.stderr, flush=True)
        await self.connection.session_update(
            session_id,
            AgentMessageChunk(
                content=TextContentBlock(type="text", text="fixture response"),
                session_update="agent_message_chunk",
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    async def load_session(self, session_id: str, cwd: str, mcp_servers: list, **kwargs):
        if self.mode == "auth-load-session":
            raise RequestError.auth_required({"token": "fixture-secret"})
        print(f"loaded:{session_id}", file=sys.stderr, flush=True)
        await self.connection.session_update(
            session_id,
            AgentMessageChunk(
                content=TextContentBlock(type="text", text="history replay"),
                session_update="agent_message_chunk",
            ),
        )

    async def cancel(self, session_id: str, **kwargs) -> None:
        self.cancelled.set()


if __name__ == "__main__":
    asyncio.run(acp.run_agent(FixtureAgent(sys.argv[1])))
