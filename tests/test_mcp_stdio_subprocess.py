"""Native stdio protocol framing through the installed command entry point."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

pytestmark = pytest.mark.skipif(
    sys.platform not in {"win32", "darwin"},
    reason="CLI serve is authorized only on Windows and supported macOS",
)


def test_cli_stdio_subprocess_initializes_lists_and_calls_tool(tmp_path: Path) -> None:
    config = tmp_path / "serverfs.toml"
    config.write_text("", encoding="utf-8")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "serverfs_mcp.cli", "serve", "--config", str(config)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    async def exercise() -> None:
        async with Client(parameters, raise_exceptions=True) as client:
            tools = await client.list_tools()
            result = await client.call_tool("list_workdirs", {})

        assert "list_workdirs" in {tool.name for tool in tools.tools}
        assert result.structured_content == {"workdirs": []}

    asyncio.run(exercise())
