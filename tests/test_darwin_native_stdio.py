"""End-to-end native stdio MCP process acceptance on supported macOS."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS native stdio acceptance")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


def test_native_serverfs_stdio_initialize_tools_read_and_stat(tmp_path: Path) -> None:
    async def exercise() -> None:
        root = tmp_path / "macOS Native Workdir"
        root.mkdir()
        (root / "hello.txt").write_bytes(b"native stdio works\n")
        config = tmp_path / "serverfs.toml"
        config.write_text(
            f'[[workdirs]]\nalias = "repo"\npath = {json.dumps(str(root))}\nread_only = true\n',
            encoding="utf-8",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "serverfs_mcp.cli", "serve", "--config", str(config)],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "ServerFS"
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "read_text_file" in names
                assert "stat_file" in names
                assert "submit_agent_task" not in names
                read = await session.call_tool(
                    "read_text_file", {"workdir": "repo", "path": "hello.txt"}
                )
                assert not read.is_error
                assert read.structured_content["content"] == "native stdio works\n"
                stat = await session.call_tool(
                    "stat_file", {"workdir": "repo", "path": "hello.txt"}
                )
                assert not stat.is_error
                assert stat.structured_content["type"] == "file"

    asyncio.run(exercise())
