"""Shared helpers for exercising tools through the MCP surface.

Tests call tools the way an agent does — ``server.call_tool`` — so a
regression test proves the behaviour of the published surface, not of an
internal helper.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import threading
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

# Error codes start with an uppercase letter and may contain uppercase
# letters, digits and underscores (for example INVALID_BASE64).
_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,}):")


def registry_for(*workdirs: Workdir) -> WorkdirRegistry:
    return WorkdirRegistry(list(workdirs))


def read_write(workdir: Workdir) -> Workdir:
    """The same workdir, authorized read-write (as WORKDIR_XX_READ_ONLY=false)."""
    return dataclasses.replace(workdir, read_only=False)


def make_server(workdir: Workdir, *, read_write_access: bool = False, **settings_kw) -> MCPServer:
    """A server over one workdir, read-only unless asked otherwise."""
    wd = read_write(workdir) if read_write_access else workdir
    settings = Settings(**settings_kw)
    wd = dataclasses.replace(
        wd,
        policy=EffectiveWorkdirPolicy(
            allow_hidden=settings.allow_hidden,
            disable_default_deny=settings.disable_default_deny,
            extra_deny_globs=settings.extra_deny_globs,
            max_read_bytes=settings.max_read_bytes,
            max_read_lines=settings.max_read_lines,
            max_write_bytes=settings.max_write_bytes,
            binary_transfer_enabled=settings.binary_transfer_enabled,
            max_binary_transfer_bytes=settings.max_binary_transfer_bytes,
            agent_mode=wd.policy.agent_mode,
            agent_runtimes=wd.policy.agent_runtimes,
        ),
    )
    return create_server(settings, registry_for(wd))


def call_success(server: MCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a tool expecting success; return its structured content."""

    async def _call():
        result = await server.call_tool(name, args)
        assert not result.is_error, result
        return result.structured_content

    return asyncio.run(_call())


def call_error(server: MCPServer, name: str, args: dict[str, Any]) -> str:
    """Call a tool expecting a ToolError; return its message."""

    async def _call():
        try:
            await server.call_tool(name, args)
        except ToolError as e:
            return str(e)
        raise AssertionError(f"expected ToolError from {name}, got success")

    return asyncio.run(_call())


def error_code(message: str) -> str:
    """The CODE: token of a coded tool error message."""
    match = _CODE_RE.search(message)
    assert match, f"no CODE: token in {message!r}"
    return match.group(1)


def call_concurrently(
    server: MCPServer, calls: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, Any]]:
    """Run tool calls from separate threads, released from one barrier.

    Returns one (outcome, payload) per call: ("ok", content) or
    ("err", message). Every thread starts its call at the same moment, so
    the serialization the server provides is what decides the outcome.
    """
    results: list[tuple[str, Any] | None] = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def worker(index: int, name: str, args: dict[str, Any]) -> None:
        barrier.wait()
        try:
            results[index] = ("ok", call_success(server, name, args))
        except ToolError as exc:
            results[index] = ("err", str(exc))
        except AssertionError as exc:  # surfaced as a failed expectation
            results[index] = ("unexpected", str(exc))

    threads = [
        threading.Thread(target=worker, args=(index, name, args), daemon=True)
        for index, (name, args) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return [r if r is not None else ("timeout", None) for r in results]


def outcomes(results: list[tuple[str, Any]]) -> list[str]:
    return [outcome for outcome, _ in results]


def error_codes(results: list[tuple[str, Any]]) -> list[str]:
    return [error_code(str(payload)) for outcome, payload in results if outcome == "err"]
