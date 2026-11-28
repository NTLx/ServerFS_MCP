"""Native I7 public HTTPS/MCP canary for two Cloudflare Tunnel routes.

Only exact I7 Core and two existing named Cloudflare Connector Scheduler tasks
may be started/stopped. Requires two already-published nonproduction CNAMEs.
No DNS/control-plane mutations, credential logging, or production lifecycle.
Always attempts scoped offline cleanup; verify live origin auth before trusting
an externally exposed MCP endpoint.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import sys
from pathlib import Path

INSTANCE = "i7-public"
CONNECTORS = ("i7-edge-a", "i7-edge-b")
HOSTS = ("workpc-v25a.ntlx.top", "workpc-v25b.ntlx.top")
PORT = 18875
PHASE = "entry"


class I7PublicRefusal(RuntimeError):
    """Constant-class-only public acceptance refusal (no secrets)."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise I7PublicRefusal(message)


def load_helper(filename: str, name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "I7 installed helper missing")
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def _base_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _initialization() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 21,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "i7-public-dual-route-canary", "version": "1"},
        },
    }


async def check_public_denials(host: str, token: str) -> None:
    import httpx2

    url = "https://" + host + "/mcp"
    headers = _base_headers(token)
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=18.0) as client:
        missing = await client.post(
            url,
            json=_initialization(),
            headers={k: v for k, v in headers.items() if k != "Authorization"},
        )
        require(missing.status_code == 401, "public missing Bearer not denied 401")
        wrong = await client.post(
            url,
            json=_initialization(),
            headers={**headers, "Authorization": "Bearer deliberate-denied-canary"},
        )
        require(wrong.status_code == 401, "public wrong Bearer not denied 401")
        # The exact Cloudflare hostname ingress or Core Host guard must refuse
        # a request with a foreign Host. Cloudflare 404 is also valid.
        foreign = await client.post(
            url,
            json=_initialization(),
            headers={**headers, "Host": "untrusted.invalid"},
        )
        require(foreign.status_code not in (200, 201, 202), "foreign Host accepted")
        origin = await client.post(
            url,
            json=_initialization(),
            headers={**headers, "Origin": "https://untrusted.invalid"},
        )
        require(origin.status_code not in (200, 201, 202), "foreign Origin accepted")
    print("I7_PUBLIC_" + host.split(".")[0].upper().replace("-", "_") + "_NEGATIVE_AUTH=PASS")


async def check_public_mcp(host: str, token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = "https://" + host + "/mcp"
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            follow_redirects=False,
            timeout=28.0,
        ) as client,
        streamable_http_client(url, http_client=client) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        require("list_workdirs" in names, "public read-only MCP tool absent")
        require(
            "submit_agent_task" not in names and "run_repo_action" not in names,
            "public origin exposed execution tools",
        )
        result = await session.call_tool("list_workdirs", {})
        require(
            getattr(result, "is_error", getattr(result, "isError", True)) is False,
            "public list_workdirs denied",
        )
        payload = getattr(result, "structured_content", None)
        if payload is None:
            payload = getattr(result, "structuredContent", None)
        if not isinstance(payload, dict):
            payload = json.loads(result.content[0].text)
        data = payload.get("result", payload)
        workdirs = data.get("workdirs", [])
        fixture = next(
            (row for row in workdirs if isinstance(row, dict) and row.get("alias") == "Fixture"),
            None,
        )
        require(
            fixture is not None
            and fixture.get("access") == "read-only"
            and fixture.get("repo_ops") is False
            and fixture.get("agent_mode") == "disabled",
            "public read-only workdir contract differs",
        )
    print("I7_PUBLIC_" + host.split(".")[0].upper().replace("-", "_") + "_MCP=PASS")


async def check_public_both(token: str) -> None:
    # Both public sessions must be separately instantiated and valid
    # concurrently against one independent Core.
    await asyncio.gather(
        check_public_mcp(HOSTS[0], token),
        check_public_mcp(HOSTS[1], token),
    )
    print("I7_PUBLIC_TWO_SEPARATE_CONCURRENT_MCP_SESSIONS=PASS")


def verify_native_runtime() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated isolated Windows required",
    )
    expected = storage.root(INSTANCE) / "envs/core/Scripts/python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True)
        and storage._sid() == current_user_sid(),
        "I7 private Core Python and matching SID required",
    )


def main() -> None:
    global PHASE
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    verify_native_runtime()
    gate = load_helper("v0250-i7-connectors-disabled-register.py", "i7_public_registration_gate")
    native = load_helper("v0250-i7-dual-connector-live-smoke.py", "i7_public_scoped_lifecycle")
    PHASE = "initial-i7-registration-postflight"
    gate.postflight()
    prod = storage.load("core")
    generation = _current_identity(prod)
    require(generation is not None, "existing production Core generation unavailable")

    key_path = storage.home("core", INSTANCE) / "http.env"
    storage.check_private(key_path, directory=False, sid=storage._sid())
    secret_record = _read_env_file(key_path)
    secret = secret_record.get("SERVERFS_HTTP_API_KEY")
    require(isinstance(secret, str) and len(secret) >= 40, "I7 private Core bearer invalid")
    del secret_record

    started: list[tuple[str, str | None]] = []
    failure: BaseException | None = None
    failure_phase = ""
    cleanup = True
    try:
        PHASE = "scoped-i7-core-start"
        started.append(("core", None))
        status = native.cli("core", "start")
        require(
            status.get("running") is True and status.get("readiness") == "verified",
            "I7 Core start refused",
        )
        for name in CONNECTORS:
            PHASE = "scoped-i7-connector-start-" + name
            started.append(("cloudflare", name))
            current = native.cli("cloudflare", "start", name)
            require(
                current.get("running") is True and current.get("readiness") == "owner_alive",
                "I7 Connector owner start refused",
            )
        require(
            all(native.active("cloudflare", name) for name in CONNECTORS),
            "two independent owners not ready",
        )
        print("I7_PUBLIC_TWO_NAMED_CONNECTORS_READY=PASS")
        PHASE = "public-https-negative-authentication"
        for host in HOSTS:
            asyncio.run(asyncio.wait_for(check_public_denials(host, secret), timeout=90.0))
        print("I7_PUBLIC_BOTH_HTTPS_BEARER_DENIALS=PASS")
        PHASE = "public-concurrent-positive-mcp"
        asyncio.run(asyncio.wait_for(check_public_both(secret), timeout=90.0))
        PHASE = "peer-survival-after-a-stop"
        b = storage.load("cloudflare", INSTANCE, CONNECTORS[1])
        b_before = _current_identity(b)
        require(b_before is not None, "Connector B process identity missing")
        require(
            native.cli("cloudflare", "stop", CONNECTORS[0]).get("state") == "Disabled",
            "Connector A stop refused",
        )
        b_after = _current_identity(b)
        require(
            b_after is not None
            and b_before.pid == b_after.pid
            and b_before.creation_time == b_after.creation_time
            and native.active("core"),
            "Connector B or Core generation changed when A stopped",
        )
        asyncio.run(asyncio.wait_for(check_public_mcp(HOSTS[1], secret), timeout=75.0))
        print("I7_PUBLIC_ROUTE_B_SURVIVES_ROUTE_A_STOP=PASS")
    except BaseException as exc:  # noqa: BLE001 - keep no secret/error text in output
        failure, failure_phase = exc, PHASE
    finally:
        PHASE = "finally-stop-only-i7-services"
        for component, connector in reversed(started):
            try:
                if not native.offline(component, connector):
                    native.cli(component, "stop", connector)
                cleanup = native.offline(component, connector) and cleanup
            except BaseException:
                cleanup = False
        del secret
    print("I7_PUBLIC_FINALLY_ALL_THREE_DISABLED=" + ("PASS" if cleanup else "FAILED"))
    require(cleanup, "I7 isolated cleanup not verified")
    PHASE = "final-i7-postflight"
    gate.postflight()
    require(
        native.offline("core") and all(native.offline("cloudflare", name) for name in CONNECTORS),
        "I7 final offline state changed",
    )
    now = _current_identity(prod)
    require(
        now is not None
        and now.pid == generation.pid
        and now.creation_time == generation.creation_time,
        "production Core process generation changed",
    )
    print("I7_PUBLIC_PRODUCTION_CORE_GENERATION=UNCHANGED")
    if failure is not None:
        PHASE = failure_phase
        raise failure
    print("I7_PUBLIC_TWO_HTTPS_MCP_ROUTES_E2E=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed, sanitized status only
        print(
            "I7_PUBLIC_CANARY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
