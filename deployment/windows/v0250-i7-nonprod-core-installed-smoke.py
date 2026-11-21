"""I7 installed nonproduction Core: one scoped local HTTP MCP lifecycle smoke.

Start/stop ONLY ServerFS v25 i7-public Core. All other protected tasks,
including i7 connectors (still absent), are read-only. No public Tunnel,
credentials export, Agent Provider, production mutation or Git operations.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

INSTANCE = "i7-public"
PORT = 18875
HOST = "i7-public.local"
PHASE = "entry"


class I7SmokeError(RuntimeError):
    """Sanitized installed Core smoke error."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I7SmokeError(message)


def helper(name: str, tag: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(tag, path)
    require(spec is not None and spec.loader is not None, "I7 source check unavailable")
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def cli(action: str) -> dict:
    require(action in ("start", "stop", "status"), "only i7 Core actions permitted")
    from serverfs_mcp.windows_v25 import storage

    python = storage.root(INSTANCE) / "envs/core/Scripts/python.exe"
    storage.check_private(python, directory=False)
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_v25.cli",
            "core",
            action,
            "--instance",
            INSTANCE,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I7 scoped Core CLI " + action + " failed")
    try:
        parsed = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise I7SmokeError("I7 Core returned invalid status") from exc
    require(isinstance(parsed, dict), "I7 Core CLI result is not a record")
    return parsed


async def http_probe(token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{PORT}/mcp"
    initialize = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i7-local-check", "version": "1"},
        },
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    async with httpx2.AsyncClient(trust_env=False, timeout=12) as http:
        wrong = await http.post(
            url,
            headers={**headers, "Authorization": "Bearer denied-fixture"},
            json=initialize,
        )
        require(wrong.status_code == 401, "I7 wrong Bearer was not rejected")
        missing = await http.post(
            url,
            headers={"Accept": headers["Accept"], "Content-Type": headers["Content-Type"]},
            json=initialize,
        )
        require(missing.status_code == 401, "I7 missing Bearer was not rejected")
        print("I7_HTTP_BEARER_WRONG_AND_MISSING=DENIED_401")
        wrong_host = await http.post(
            url, headers={**headers, "Host": "unexpected.example.test"}, json=initialize
        )
        require(wrong_host.status_code == 421, "I7 unapproved Host was not rejected")
        origin = await http.post(
            url,
            headers={**headers, "Origin": "https://unexpected.example.test"},
            json=initialize,
        )
        require(origin.status_code != 200, "I7 hostile Origin was accepted")
        print("I7_HTTP_HOST_AND_ORIGIN=DENIED")

    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token, "Host": HOST},
            trust_env=False,
            timeout=20,
        ) as client,
        streamable_http_client(url, http_client=client) as (reader, writer),
    ):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = {item.name for item in (await session.list_tools()).tools}
            require("list_workdirs" in tools, "I7 MCP file-only inventory unavailable")
            listed = await session.call_tool("list_workdirs", {})
            require(
                getattr(listed, "is_error", getattr(listed, "isError", True)) is False,
                "I7 authorized list_workdirs failed",
            )
            mapping = getattr(listed, "structured_content", None)
            if mapping is None:
                mapping = getattr(listed, "structuredContent", None)
            if not isinstance(mapping, dict):
                mapping = json.loads(listed.content[0].text)
            data = mapping.get("result", mapping)
            entries = data.get("workdirs", [])
            fixture = next((item for item in entries if item.get("alias") == "Fixture"), None)
            require(
                isinstance(fixture, dict)
                and fixture.get("access") == "read-only"
                and fixture.get("repo_ops") is False
                and fixture.get("agent_mode") == "disabled",
                "I7 MCP public file policy differs",
            )
            require(
                not any(name in tools for name in ("submit_agent_task", "run_repo_action")),
                "I7 file-only Core exposes unauthorized execution tools",
            )
    print("I7_NATIVE_HTTP_AUTHENTICATED_READ_ONLY_MCP=PASS")


def same_generation(a, b) -> bool:
    return a is not None and b is not None and a.pid == b.pid and a.creation_time == b.creation_time


def main() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated I7 smoke Python -I -B required",
    )
    private = storage.root(INSTANCE) / "envs/core/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == private.resolve(strict=True),
        "I7 smoke must use installed private Core Python",
    )
    require(current_user_sid() == storage._sid(), "I7 owner SID differs")
    gate = helper("v0250-i7-connectors-disabled-register.py", "i7_registered_connector_gate")
    PHASE = "initial-installed-postflight"
    gate.postflight()
    prod = storage.load("core")
    prod_identity = _current_identity(prod)
    require(prod_identity is not None, "production Core identity not verified")
    key_file = storage.home("core", INSTANCE) / "http.env"
    storage.check_private(key_file, directory=False)
    auth = _read_env_file(key_file)
    token = auth.get("SERVERFS_HTTP_API_KEY")
    require(isinstance(token, str) and len(token) >= 40, "I7 private key invalid")
    del auth

    started = False
    failure: BaseException | None = None
    failed_phase = ""
    cleanup = False
    try:
        PHASE = "scoped-i7-core-start"
        started = True
        status = cli("start")
        require(
            status.get("running") is True and status.get("readiness") == "verified",
            "I7 isolated Core readiness not verified",
        )
        print("I7_INSTALLED_CORE_START=VERIFIED")
        PHASE = "live-local-http-mcp"
        asyncio.run(asyncio.wait_for(http_probe(token), timeout=60))
    except BaseException as exc:
        failure, failed_phase = exc, PHASE
    finally:
        PHASE = "scope-only-core-stop"
        if started:
            try:
                state = cli("stop")
                cleanup = (
                    state.get("running") is False
                    and state.get("enabled") is False
                    and state.get("state") == "Disabled"
                )
            except BaseException:
                cleanup = False
    del token
    print("I7_PRIVATE_CORE_STOP=" + ("VERIFIED" if cleanup else "NOT_VERIFIED"))
    require(cleanup, "I7 Core cleanup failed")
    PHASE = "post-cleanup-installed-postflight"
    gate.postflight()
    require(
        same_generation(prod_identity, _current_identity(prod)),
        "production Core process generation unexpectedly changed",
    )
    print("I7_PRODUCTION_CORE_GENERATION=UNCHANGED")
    if failure is not None:
        PHASE = failed_phase
        raise failure
    print("I7_NATIVE_INSTALLED_FILE_ONLY_CORE_HTTP=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never print URL, token, private path
        print(
            "I7_NATIVE_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
