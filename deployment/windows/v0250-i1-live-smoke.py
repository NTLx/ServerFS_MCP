"""Windows I1 live fixture acceptance: start/verify/stop ONLY i1-smoke Core.

Run under the NEW private i1-smoke Core Python with -I -B after a Disabled
registration. Never touches or terminates another ServerFS Task Scheduler
entry, provider process, tunnel, guard, credential or data root.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

INSTANCE = "i1-smoke"
HOST = "i1-smoke.local"
PORT = 18868
TASK = "ServerFS v25 i1-smoke Core"
PHASE = "entry"
SCRIPT = r"""
$ErrorActionPreference='Stop'
$names=@(
 'ServerFS Native Tunnel',
 'ServerFS v25 Core',
 'ServerFS v25 Cloudflare Connector',
 'ServerFS v25 Agent Bridge',
 'ServerFS v25 OpenAI Connector'
)
$records=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
 $_.TaskPath -eq '\' -and $names -contains $_.TaskName
} | ForEach-Object {
 [PSCustomObject]@{
  name=$_.TaskName
  state=[string]$_.State
  enabled=[bool]$_.Settings.Enabled
 }
})
if ($records.Count -ne $names.Count) { exit 13 }
$records | Sort-Object name | ConvertTo-Json -Compress
"""


class SmokeError(Exception):
    """No secrets, host paths or opaque provider data in failure descriptions."""


def require(value: bool, label: str) -> None:
    if not value:
        raise SmokeError(label)


def mark(phase: str) -> None:
    """Record a fixed, non-secret test phase for a redacted host failure."""
    global PHASE
    PHASE = phase


def production_snapshot() -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
        capture_output=True,
        timeout=25,
        check=False,
    )
    require(result.returncode == 0, "production task inventory inaccessible")
    try:
        rows = json.loads(result.stdout.decode("utf-8-sig"))
        require(
            isinstance(rows, list) and len(rows) == 5, "production task inventory is incomplete"
        )
        status = {item["name"]: item for item in rows}
        for name in (
            "ServerFS Native Tunnel",
            "ServerFS v25 Core",
            "ServerFS v25 Cloudflare Connector",
        ):
            require(
                status[name]["state"] == "Running" and status[name]["enabled"],
                "production task baseline is unverified",
            )
        for name in ("ServerFS v25 Agent Bridge", "ServerFS v25 OpenAI Connector"):
            require(
                status[name]["state"] != "Running" and not status[name]["enabled"],
                "disabled owner baseline is unverified",
            )
        return rows
    except (KeyError, TypeError, ValueError) as exc:
        raise SmokeError("production task inventory is malformed") from exc


def lifecycle_error_category(stderr: bytes) -> str:
    """Classify only known fixed CLI errors; never emit its raw output."""
    detail = stderr.decode("utf-8", errors="replace")
    for marker, category in (
        ("component start refused at ready", "start-readiness"),
        ("component start refused at run", "start-run"),
        ("component start refused at enable", "start-enable"),
        ("registered service task does not match owner contract", "task-identity"),
        ("Scheduler current-user state query is unavailable", "scheduler-state"),
        ("component stop is unverified", "stop-readiness"),
        ("component private input cannot be verified", "private-input"),
    ):
        if marker in detail:
            return category
    return "other"


def cli(action: str) -> dict:
    require(action in ("status", "start", "stop"), "lifecycle action not allowlisted")
    result = subprocess.run(
        [
            sys.executable,
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
        timeout=32,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"I1_CLI_{action.upper()}=FAILED category={lifecycle_error_category(result.stderr)}",
            file=sys.stderr,
        )
        raise SmokeError("isolated Core lifecycle operation refused")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SmokeError("isolated Core returned invalid status") from exc


def http_status(*, authorization: str | None = None, origin: str | None = None) -> int:
    headers = {
        "Host": f"127.0.0.1:{PORT}",
        "Accept": "application/json, text/event-stream",
    }
    if authorization is not None:
        headers["Authorization"] = "Bearer " + authorization
    if origin is not None:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        conn.request("GET", "/mcp", headers=headers)
        response = conn.getresponse()
        return response.status
    finally:
        conn.close()


def private_key() -> str:
    from serverfs_mcp.windows_service import _read_env_file
    from serverfs_mcp.windows_v25 import storage

    manifest = storage.load("core", INSTANCE)
    require(
        manifest.settings["port"] == PORT and manifest.settings["hostname"] == HOST,
        "fixture Core identity is not the approved port/host",
    )
    path = Path(str(manifest.settings["auth_file"]))
    require(
        path.parent == storage.home("core", INSTANCE) and path.name == "http.env",
        "fixture auth file is not in its private service home",
    )
    storage.check_private(path, directory=False, sid=manifest.sid)
    values = _read_env_file(path)
    require(
        values.get("SERVERFS_OAUTH_ENABLED") == "false", "unexpected fixture authentication mode"
    )
    key = values.get("SERVERFS_HTTP_API_KEY", "")
    require(len(key) >= 40, "fixture private API key is invalid")
    return key


def result_mapping(response) -> dict:
    # MCP SDK 2.3 exposes Python snake_case result fields; older fixtures
    # may provide the wire-format camelCase aliases.
    error = getattr(response, "is_error", getattr(response, "isError", False))
    require(error is False or error is None, "file-only MCP operation was rejected")
    structured = getattr(response, "structured_content", None)
    if structured is None:
        structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        if isinstance(structured.get("result"), dict):
            return structured["result"]
        return structured
    for item in response.content:
        value = getattr(item, "text", None)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                if isinstance(parsed.get("result"), dict):
                    return parsed["result"]
                return parsed
    raise SmokeError("MCP tool did not return structured file evidence")


async def exercise_mcp(token: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = f"http://127.0.0.1:{PORT}/mcp"
    # Only this probe's own client environment is scrubbed; no host-wide change.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)
    mark("mcp-connect")
    # MCP SDK 2.3 uses a caller-owned httpx2.AsyncClient for auth headers
    # and yields (reader, writer), not the legacy three-item tuple.
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as http_client,
        streamable_http_client(url, http_client=http_client) as (reader, writer),
    ):
        async with ClientSession(reader, writer) as session:
            mark("mcp-initialize")
            await session.initialize()
            mark("mcp-list-tools")
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            for name in ("list_workdirs", "create_text_file", "read_text_file", "delete_file"):
                require(name in names, "essential MCP file tool is absent")
            require(
                not any(name in names for name in ("submit_agent_task", "run_repo_action")),
                "file-only Core exposed an Agent or repository execution tool",
            )
            mark("mcp-list-workdirs")
            response = await session.call_tool("list_workdirs", {})
            error = getattr(response, "is_error", getattr(response, "isError", False))
            print("I1_WORKDIR_TOOL=" + ("REJECTED" if error is True else "RETURNED"))
            roots = result_mapping(response)
            workdirs = roots.get("workdirs")
            print("I1_WORKDIR_PAYLOAD=" + ("LIST" if isinstance(workdirs, list) else "INVALID"))
            fixture = (
                next(
                    (
                        item
                        for item in workdirs
                        if isinstance(item, dict) and item.get("alias") == "Fixture"
                    ),
                    None,
                )
                if isinstance(workdirs, list)
                else None
            )
            print("I1_FIXTURE_ALIAS=" + ("FOUND" if fixture is not None else "MISSING"))
            if isinstance(fixture, dict):
                for field, expected in (
                    ("access", "read-write"),
                    ("agent_mode", "disabled"),
                    ("repo_ops", False),
                ):
                    print(
                        "I1_FIXTURE_"
                        + field.upper()
                        + "="
                        + ("EXPECTED" if fixture.get(field) == expected else "CHANGED")
                    )
            require(
                isinstance(fixture, dict)
                and fixture.get("access") == "read-write"
                and fixture.get("agent_mode") == "disabled"
                and fixture.get("repo_ops") is False,
                "fixture workdir policy was not found",
            )

            # A failed earlier probe may have left its own file. Never adopt,
            # overwrite or remove that artifact during a subsequent attempt.
            path = f"i1-test-{secrets.token_hex(12)}.txt"
            content = "v0250-i1-isolated-file-only-test\n"
            mark("mcp-create-file")
            created = result_mapping(
                await session.call_tool(
                    "create_text_file", {"workdir": "Fixture", "path": path, "content": content}
                )
            )
            revision = created.get("revision")
            require(isinstance(revision, str) and revision, "created file lacks revision proof")
            mark("mcp-read-file")
            opened = result_mapping(
                await session.call_tool(
                    "read_text_file",
                    {"workdir": "Fixture", "path": path},
                )
            )
            require(content in opened.get("content", ""), "MCP file read is not consistent")
            mark("mcp-delete-file")
            removed = result_mapping(
                await session.call_tool(
                    "delete_file",
                    {"workdir": "Fixture", "path": path, "expected_revision": revision},
                )
            )
            require(removed.get("deleted") is True, "MCP file deletion was not confirmed")


def main() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity

    require(
        sys.platform == "win32" and bool(sys.flags.isolated and sys.dont_write_bytecode),
        "native private -I -B Python is required",
    )
    mark("interpreter-identity")
    expected = storage.pinned_pythonw("core", INSTANCE)
    require(
        Path(sys.executable).resolve(strict=True).parent == expected.parent,
        "probe is not running under the isolated fixture Python environment",
    )
    mark("production-task-preflight")
    before = production_snapshot()
    mark("production-process-preflight")
    baseline = storage.load("core")
    baseline_identity = _current_identity(baseline)
    require(baseline_identity is not None, "production Core PID evidence is unavailable")
    mark("fixture-task-preflight")
    initial = cli("status")
    require(
        initial.get("registered") is True
        and initial.get("task_name") == TASK
        and not initial.get("enabled")
        and not initial.get("running"),
        "fixture was not registered Disabled",
    )
    mark("fixture-private-auth")
    key = private_key()
    started = False
    error: BaseException | None = None
    stopped = False
    failed_phase = ""
    try:
        started = True
        mark("fixture-start")
        running = cli("start")
        mark("fixture-readiness")
        require(
            running.get("running") is True
            and running.get("readiness") == "verified"
            and running.get("task_name") == TASK,
            "fixture Core process or listener ownership is not verified",
        )
        mark("http-no-credentials")
        require(http_status() == 401, "unauthenticated request was not rejected")
        mark("http-invalid-credentials")
        require(
            http_status(authorization="not-the-fixture-key") == 401,
            "incorrect credentials were not rejected",
        )
        mark("http-origin-refusal")
        try:
            origin_status = http_status(
                authorization=key,
                origin="https://untrusted.example",
            )
        except (OSError, http.client.HTTPException) as exc:
            print(
                "I1_HTTP_ORIGIN=REQUEST_FAILED type=" + type(exc).__name__,
                file=sys.stderr,
            )
            raise
        print(f"I1_HTTP_ORIGIN_STATUS={origin_status}")
        require(origin_status == 403, "untrusted Origin must return explicit HTTP 403")
        asyncio.run(asyncio.wait_for(exercise_mcp(key), timeout=60))
        print("I1_LOOPBACK_AUTH_AND_FILE_MCP=PASS")
    except BaseException as exc:
        error = exc
        failed_phase = PHASE
    finally:
        if started:
            try:
                disabled = cli("stop")
                stopped = (
                    disabled.get("state") == "Disabled"
                    and not disabled.get("enabled")
                    and not disabled.get("running")
                    and cli("status").get("readiness") == "offline"
                )
            except Exception:
                stopped = False
    if stopped:
        print("I1_FIXTURE_STOP=VERIFIED")
    else:
        mark("fixture-stop")
        print("I1_FIXTURE_STOP=NOT_VERIFIED")
    require(stopped, "fixture Core stop is unverified; do not terminate a foreign process")
    mark("production-task-postflight")
    after = production_snapshot()
    require(before == after, "a production Task Scheduler baseline changed")
    mark("production-process-postflight")
    current = _current_identity(baseline)
    require(
        current is not None
        and current.pid == baseline_identity.pid
        and current.creation_time == baseline_identity.creation_time,
        "production Core process generation changed during fixture probe",
    )
    if error is not None:
        mark(failed_phase)
        raise SmokeError("isolated Core HTTP/MCP validation did not pass") from error
    print("I1_ISOLATED_TASK_STOP=PASS")
    print("I1_PRODUCTION_TASKS_AND_CORE_PID=UNCHANGED")
    print("I1_NATIVE_DUAL_CORE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(
            "I1_NATIVE_SMOKE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
