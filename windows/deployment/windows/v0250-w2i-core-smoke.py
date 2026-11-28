"""One-shot native Windows isolated Core-only activation and verification.

Run only via v0250-w2i-core-smoke.ps1 after normal current-user preflight.
Starts the *registered and owned* v0.25 Core task, checks its independent
loopback owner and unauthenticated 401, then stops only that Core task and
verifies it is disabled/offline again. Never starts a Bridge or Connector.
Only writes a new redacted Markdown report. No credentials or path logging.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PORT = 18766
_HOSTNAME = "workpc-serverfs.ntlx.top"
_OTHER = ("bridge", "cloudflare", "openai")
_ALLOWED_ENV = {
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "PROGRAMDATA",
}


class W2iError(Exception):
    """Credential-free W2i gate refusal."""


def _classify_cli_failure(stderr: str) -> str:
    """Allowlist-only lifecycle reason, never raw paths/arguments/credentials."""
    categories = (
        ("Core activation failed; rollback incomplete", "core-start-rollback-incomplete"),
        ("Core registered task identity has drifted", "core-task-contract-drift"),
        ("Core scheduled task could not be stopped", "core-scheduler-stop-refused"),
        ("Core Task Scheduler mutation failed", "core-scheduler-mutation-refused"),
        ("Core lifecycle proof timed out", "core-lifecycle-proof-timeout"),
        ("Core previous generation history is unverified", "core-generation-unverified"),
        ("Core previous generation is still alive", "core-generation-active"),
        ("Core previous process termination is unverified", "core-generation-unproven"),
        ("Core service private Windows DACL validation failed", "core-DACL-refused"),
        ("Core registered owner proof is unavailable", "core-owner-proof-refused"),
        ("Core process is active but listener owner is unverified", "core-listener-unverified"),
        ("Core task did not become disabled", "core-disable-refused"),
        ("Core Task Scheduler did not remain disabled", "core-disable-unverified"),
    )
    for signature, label in categories:
        if signature in stderr:
            return label
    return "unclassified-redacted-cli-error"


def _invoke_component(env_home: Path, component: str, action: str) -> dict[str, Any]:
    exe = env_home / "Scripts" / "python.exe"
    if not exe.is_file():
        raise W2iError("dedicated component interpreter is unavailable")
    traced = component == "core" and action in ("start", "stop")
    trace_script = (
        Path(__file__).resolve().with_name("v0250-w2p-core-lifecycle-trace.py") if traced else None
    )
    if trace_script is not None and not trace_script.is_file():
        raise W2iError("private Core lifecycle tracer is unavailable")
    command = (
        [str(exe), "-I", "-B", str(trace_script), action]
        if traced
        else [
            str(exe),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_component_cli",
            component,
            action,
        ]
    )
    outcome = subprocess.run(
        command,
        cwd=env_home,
        env={k: v for k, v in os.environ.items() if k.upper() in _ALLOWED_ENV},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=75,
    )
    if traced:
        try:
            payload = json.loads(outcome.stdout)
            if (
                not isinstance(payload, dict)
                or type(payload.get("ok")) is not bool
                or not isinstance(payload.get("trace"), list)
                or len(payload["trace"]) > 48
                or any(
                    not isinstance(item, str)
                    or len(item) > 120
                    or not re.fullmatch(r"[A-Za-z0-9 _:=;.-]+", item)
                    or item.split(":", 1)[0]
                    not in ("start", "stop", "taskcall", "owner", "wait", "status")
                    for item in payload["trace"]
                )
                or not isinstance(payload.get("reason"), str)
                or len(payload["reason"]) > 100
                or not re.fullmatch(r"[A-Za-z0-9 _:=;.-]+", payload["reason"])
            ):
                raise ValueError("untrusted Core trace response")
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise W2iError("Core tracer did not return a verified redacted report") from exc
        # The trace source emits only fixed operation names and numeric return
        # codes. Never include arbitrary stderr/argv/private status fields.
        trace = ";".join(payload["trace"][-28:])
        if outcome.returncode != 0 or not payload["ok"]:
            raise W2iError(
                f"{component}-{action}: exit={outcome.returncode}; "
                f"reason={payload['reason']}; trace={trace}"
            )
        status = payload.get("status")
        if not isinstance(status, dict):
            raise W2iError("Core tracer returned an invalid status")
        return status
    if outcome.returncode != 0:
        reason = _classify_cli_failure(outcome.stderr)
        # No raw stderr, task XML, secret, file path, user SID or argv is ever
        # returned to diagnostics; the exit code and known classification are.
        raise W2iError(f"{component}-{action}: exit={outcome.returncode}; reason={reason}")
    try:
        status = json.loads(outcome.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise W2iError(f"{component} returned an invalid status") from exc
    if not isinstance(status, dict):
        raise W2iError("component status is not a JSON object")
    return status


def _http_unauthenticated(host: str) -> int:
    """Request only authentication rejection, not MCP operations or sessions."""
    client = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=8)
    try:
        client.request(
            "GET",
            "/mcp",
            headers={
                "Host": host,
                "Accept": "application/json, text/event-stream",
                "Connection": "close",
            },
        )
        response = client.getresponse()
        if response.status != 401:
            raise W2iError("independent Core did not reject unauthenticated request")
        # Do not collect response body or any auth challenge metadata.
        return response.status
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise W2iError("independent Core HTTP authentication probe failed") from exc
    finally:
        client.close()


def _verify_other_disabled(base: Path) -> None:
    for component in _OTHER:
        env_home = (
            base / f"{component}-connector-env"
            if component != "bridge"
            else base / "bridge-owner-env"
        )
        data = _invoke_component(env_home, component, "status")
        if not (
            data.get("registered") is True
            and data.get("task_valid") is True
            and data.get("enabled") is False
        ):
            raise W2iError("another independent component task is not disabled")


def _check_core(data: dict[str, Any], *, running: bool) -> None:
    if not (
        data.get("registered") is True
        and data.get("task_valid") is True
        and data.get("core_alive") is running
        and data.get("enabled") is running
    ):
        raise W2iError("Core registered and process state do not match the expected stage")
    if running and data.get("listener_owner") != "verified":
        raise W2iError("Core TCP listener owner was not verified")


def _stage(events: list[str]) -> None:
    from serverfs_mcp.native_lifecycle import window_local_app_data

    base = window_local_app_data() / "ServerFS"
    core = base / "core-owner-env"
    _verify_other_disabled(base)
    before = _invoke_component(core, "core", "status")
    _check_core(before, running=False)
    events.append("before: Core registered/disabled/offline, other components disabled")

    attempted = False
    try:
        attempted = True
        events.append("Core start: invoked exact registered Core component")
        started = _invoke_component(core, "core", "start")
        events.append("Core start: CLI returned success")
        _check_core(started, running=True)
        events.append("independent Core: owned process and listener verified")
        _http_unauthenticated("127.0.0.1:18766")
        events.append("Core HTTP GET /mcp: unauthenticated 401 verified")
        _verify_other_disabled(base)
        events.append("Bridge and connectors: remain disabled during Core-only test")
    except Exception as exc:
        # Capture the primary failure before finally attempts Core cleanup;
        # otherwise a second stop exception masks the actual start failure.
        reason = str(exc) if isinstance(exc, W2iError) else type(exc).__name__
        events.append(f"Core start/HTTP stage: FAILED ({reason})")
        raise
    finally:
        if attempted:
            # This is the EXACT Core component CLI; it verifies registered
            # owner and disables its task before calling its own /End.
            # A failure here must be reported independently; the PowerShell
            # outer finally then disables only the exact new Scheduler task.
            events.append("Core stop: attempting owned task stop")
            try:
                stopped = _invoke_component(core, "core", "stop")
                _check_core(stopped, running=False)
                events.append("Core stopped: disabled/offline with owned stop contract")
                _verify_other_disabled(base)
                events.append("Bridge and connectors: still disabled after Core stop")
            except Exception as exc:
                reason = str(exc) if isinstance(exc, W2iError) else type(exc).__name__
                events.append(f"Core owned stop: FAILED ({reason})")
                raise


def main() -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("W2I_CORE=WINDOWS_REQUIRED", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    report = (
        repo
        / "docs"
        / "diagnostics"
        / ("v0250-windows-w2i-core-smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".md")
    )
    events: list[str] = []
    passed = False
    try:
        _stage(events)
        passed = True
    except Exception as exc:
        events.append(f"Core-only gate FAILED ({type(exc).__name__}); no unverified cleanup")
    lines = [
        "# Windows v0.25.0 W2i — isolated Core-only smoke",
        "",
        "Overall: " + ("PASS" if passed else "NOT_ACCEPTED"),
        "UTC: " + datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "",
        "## Safe observed gate facts",
        *("- " + line for line in events),
        "",
        "The only eligible lifecycle mutation was the exact new Core task.",
        "No Bridge, Connector, legacy task, Agent Provider or existing",
        "workdir was intentionally modified. No secret, SID, private path,",
        "HTTP response body or subprocess output was collected or logged.",
    ]
    try:
        with report.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    except OSError:
        print("W2I_CORE=REPORT_WRITE_FAILED", file=sys.stderr)
        return 2
    print(f"W2i Core smoke report: {report}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
