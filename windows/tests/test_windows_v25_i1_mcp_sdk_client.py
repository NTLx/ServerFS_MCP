"""I1 owner smoke uses the installed MCP 2.3 Streamable HTTP client contract.

No Windows service, credential store, HTTP listener or Task Scheduler call.
The mock exercises actual I1 MCP CRUD control flow and checks that bearer
credentials are passed only through a caller-owned HTTP client.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx2
import mcp
import pytest
from mcp.client import streamable_http
from mcp_types import CallToolResult

_SOURCE = Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i1-live-smoke.py"


def _smoke():
    spec = importlib.util.spec_from_file_location("i1_smoke_sdk_contract", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_sdk_exposes_two_stream_transport_and_owned_http_client() -> None:
    assert callable(streamable_http.streamable_http_client)
    sig = inspect.signature(streamable_http.streamable_http_client)
    assert "http_client" in sig.parameters
    assert "headers" not in sig.parameters
    source = _SOURCE.read_text(encoding="utf-8")
    assert "streamablehttp_client" not in source
    assert "streamable_http_client(url, http_client=http_client)" in source
    assert "trust_env=False" in source
    assert "as (reader, writer)" in source
    assert "as (reader, writer, _session_id)" not in source


def test_mcp_sdk_snake_case_tool_result_is_accepted_and_error_is_denied() -> None:
    smoke = _smoke()
    answer = CallToolResult(
        content=[],
        structured_content={"workdirs": [{"alias": "Fixture", "access": "read-write"}]},
        is_error=False,
    )
    assert smoke.result_mapping(answer)["workdirs"][0]["alias"] == "Fixture"
    refused = CallToolResult(content=[], structured_content={}, is_error=True)
    with pytest.raises(smoke.SmokeError, match="rejected"):
        smoke.result_mapping(refused)


def test_i1_mcp_crud_uses_caller_owned_authenticated_client(monkeypatch) -> None:
    smoke = _smoke()
    seen = []
    token = "TEST_TOKEN_DO_NOT_LOG"

    class FakeHTTPClient:
        def __init__(self, *, headers, trust_env):
            assert headers == {"Authorization": "Bearer " + token}
            assert trust_env is False
            seen.append("http-created")

        async def __aenter__(self):
            seen.append("http-open")
            return self

        async def __aexit__(self, *args):
            seen.append("http-close")

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client):
        assert url == f"http://127.0.0.1:{smoke.PORT}/mcp"
        assert isinstance(http_client, FakeHTTPClient)
        seen.append("transport-open")
        try:
            yield object(), object()
        finally:
            seen.append("transport-close")

    class FakeClientSession:
        def __init__(self, reader, writer):
            assert reader is not None and writer is not None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            seen.append("initialize")

        async def list_tools(self):
            names = ("list_workdirs", "create_text_file", "read_text_file", "delete_file")
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        async def call_tool(self, name, args):
            seen.append(name)
            if name == "list_workdirs":
                result = {
                    "workdirs": [
                        {
                            "alias": "Fixture",
                            "access": "read-write",
                            "agent_mode": "disabled",
                            "repo_ops": False,
                        }
                    ]
                }
            elif name == "create_text_file":
                assert args["workdir"] == "Fixture"
                assert args["path"].startswith("i1-test-")
                assert args["content"] == "v0250-i1-isolated-file-only-test\n"
                result = {"revision": "v1:synthetic"}
            elif name == "read_text_file":
                assert args["workdir"] == "Fixture"
                result = {"content": "v0250-i1-isolated-file-only-test\n"}
            elif name == "delete_file":
                assert args["expected_revision"] == "v1:synthetic"
                result = {"deleted": True}
            else:
                raise AssertionError(f"unexpected MCP method: {name}")
            return SimpleNamespace(isError=False, structuredContent={"result": result}, content=[])

    monkeypatch.setattr(httpx2, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    asyncio.run(smoke.exercise_mcp(token))
    assert seen == [
        "http-created",
        "http-open",
        "transport-open",
        "initialize",
        "list_workdirs",
        "create_text_file",
        "read_text_file",
        "delete_file",
        "transport-close",
        "http-close",
    ]
