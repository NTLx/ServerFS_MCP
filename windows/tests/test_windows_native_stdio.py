"""End-to-end native stdio MCP process test on Windows with the Rust backend."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native stdio acceptance")
pytest.importorskip(
    "serverfs_windows_native", reason="serverfs-windows-native wheel/pyd not installed"
)

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


def test_native_serverfs_stdio_initialize_tools_read_and_stat(tmp_path: Path) -> None:
    async def exercise() -> None:
        root = tmp_path / "Windows Native Workdir"
        root.mkdir()
        (root / "hello.txt").write_bytes(b"native stdio works\r\n")
        config = tmp_path / "serverfs.toml"
        config.write_text(
            f'[[workdirs]]\nalias = "repo"\npath = {json.dumps(str(root))}\nread_only = true\n',
            encoding="utf-8",
        )
        report_path = tmp_path / "child-environment.json"
        sitecustomize = tmp_path / "sitecustomize.py"
        sitecustomize.write_text(
            "import json, os\n"
            "sentinels = {\n"
            "    'CONTROL_PLANE_API_KEY': 'api-sentinel',\n"
            "    'CONTROL_PLANE_HTTP_PROXY': 'control-proxy-sentinel',\n"
            "    'TUNNEL_CLIENT_HTTP_PROXY': 'tunnel-proxy-sentinel',\n"
            "    'OPENAI_API_KEY': 'openai-sentinel',\n"
            "    'MCP_COMMAND': 'mcp-sentinel',\n"
            "    'SERVERFS_PROXY_PASSWORD': 'serverfs-proxy-sentinel',\n"
            "    'HTTP_PROXY': 'http-proxy-sentinel',\n"
            "    'https_proxy': 'https-proxy-sentinel',\n"
            "    'ALL_PROXY': 'all-proxy-sentinel',\n"
            "    'NO_PROXY': 'upper-no-proxy-sentinel',\n"
            "    'no_proxy': 'no-proxy-sentinel',\n"
            "}\n"
            "if not any(os.environ.get(k) == v for k, v in sentinels.items()):\n"
            "    with open(os.environ['SERVERFS_TEST_ENV_REPORT'], 'w', encoding='utf-8') as f:\n"
            "        json.dump({k: k in os.environ for k in sentinels}, f)\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "SERVERFS_TEST_ENV_REPORT": str(report_path),
                "CONTROL_PLANE_API_KEY": "api-sentinel",
                "CONTROL_PLANE_HTTP_PROXY": "control-proxy-sentinel",
                "TUNNEL_CLIENT_HTTP_PROXY": "tunnel-proxy-sentinel",
                "OPENAI_API_KEY": "openai-sentinel",
                "MCP_COMMAND": "mcp-sentinel",
                "SERVERFS_PROXY_PASSWORD": "serverfs-proxy-sentinel",
                "HTTP_PROXY": "http-proxy-sentinel",
                "https_proxy": "https-proxy-sentinel",
                "ALL_PROXY": "all-proxy-sentinel",
                "NO_PROXY": "upper-no-proxy-sentinel",
                "no_proxy": "no-proxy-sentinel",
            }
        )
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(tmp_path), env.get("PYTHONPATH", "")) if part
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "serverfs_mcp.supervisor", "--config", str(config)],
            env=env,
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "ServerFS"
                assert json.loads(report_path.read_text(encoding="utf-8")) == {
                    key: False
                    for key in (
                        "CONTROL_PLANE_API_KEY",
                        "CONTROL_PLANE_HTTP_PROXY",
                        "TUNNEL_CLIENT_HTTP_PROXY",
                        "OPENAI_API_KEY",
                        "MCP_COMMAND",
                        "SERVERFS_PROXY_PASSWORD",
                        "HTTP_PROXY",
                        "https_proxy",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "no_proxy",
                    )
                }
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "read_text_file" in names
                assert "stat_file" in names
                assert "submit_agent_task" not in names
                read = await session.call_tool(
                    "read_text_file", {"workdir": "repo", "path": "hello.txt"}
                )
                assert not read.is_error
                assert read.structured_content["content"] == "native stdio works\r\n"
                stat = await session.call_tool(
                    "stat_file", {"workdir": "repo", "path": "hello.txt"}
                )
                assert not stat.is_error
                assert stat.structured_content["type"] == "file"

    asyncio.run(exercise())
