from __future__ import annotations

import asyncio

from mcp import Client

from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import WorkdirRegistry


async def _exercise(mode: str) -> tuple[str, set[str], dict | None]:
    server = create_server(Settings(), WorkdirRegistry([]))
    async with Client(server, mode=mode, raise_exceptions=True) as client:
        tools = await client.list_tools()
        result = await client.call_tool("list_workdirs", {})
        return (
            str(client.protocol_version),
            {tool.name for tool in tools.tools},
            result.structured_content,
        )


async def _modern_capabilities():
    server = create_server(Settings(), WorkdirRegistry([]))
    async with Client(server, raise_exceptions=True) as client:
        return str(client.protocol_version), client.server_capabilities


def test_modern_protocol_uses_portable_serverfs_surface() -> None:
    protocol, tools, content = asyncio.run(_exercise("2026-07-28"))

    assert protocol == "2026-07-28"
    assert len(tools) == 11
    assert "list_workdirs" in tools
    assert "upload_binary_file" not in tools
    assert "submit_agent_task" not in tools
    assert "diagnostics.snapshot" not in tools
    assert "diagnostics.task" not in tools
    assert content == {"workdirs": []}


def test_legacy_protocol_uses_same_portable_serverfs_surface() -> None:
    protocol, tools, content = asyncio.run(_exercise("legacy"))

    assert protocol == "2025-11-25"
    assert len(tools) == 11
    assert "list_workdirs" in tools
    assert "upload_binary_file" not in tools
    assert "submit_agent_task" not in tools
    assert content == {"workdirs": []}


def test_modern_capabilities_do_not_advertise_dynamic_subscriptions() -> None:
    protocol, capabilities = asyncio.run(_modern_capabilities())

    assert protocol == "2026-07-28"
    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is False
    assert capabilities.resources is not None
    assert capabilities.resources.list_changed is False
    assert capabilities.resources.subscribe is False
