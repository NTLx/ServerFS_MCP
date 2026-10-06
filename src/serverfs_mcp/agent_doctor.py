"""Agent-aware diagnostics for ``serverfs doctor`` (v0.11 Phase D, §13, §15 D8).

Rules this module must never break, all of which tighten the v0.10 Tunnel contract rather than
relaxing it:

- **read-only.** Every check here inspects configuration, derives a path, or opens a socket for a
  reachability probe. Nothing creates the Agent data tree, renders a Bridge config, writes a lease,
  or
  starts a Bridge or a provider. A diagnostic that made the deployment startable would be a launcher
  wearing a report's clothes.
- **never a launcher.** If the Bridge is not running, doctor says so and says the supervisor owns
  owns starting it. When it *is* running, doctor may make a read-only authenticated probe,
  because that is an observation of an existing process, not a decision to create one.
- **stdout stays empty.** Lines go to the writer (stderr in the CLI), because stdout is reserved for
  MCP frames (§27).
- **the endpoint never appears.** Phase 0F §8 measured that a provider's own health report
  can report reachability without its endpoint, and that is the shape used here. The permitted
  vocabulary is enabled/disabled, source, authentication, local bypass and reachability. Host,
  port, URL, userinfo, credential and the raw environment value are all absent, including from
  exception text, which is why the reachability probe normalizes failures into a fixed vocabulary.

A disabled Agent is a normal configuration, so it is reported as a *note* and never as WARN or
FAIL. A FAIL. A doctor that cried wolf about the default state would train operators to ignore it.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .native_endpoint import NativeEndpointError, derive_pipe_name

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"
NOTE = "note"

#: The complete permitted proxy vocabulary. Anything outside this set is a leak.
PROXY_ENABLED = "enabled"
PROXY_SOURCE = "source"
PROXY_AUTH = "authentication"
PROXY_BYPASS = "mandatory local bypass"
PROXY_REACH = "reachability"


def _reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """DNS/TCP reachability only. Returns (ok, normalized-state).

    No provider request, no credential, no HTTP. The failure vocabulary is fixed at the call site so
    an exception carrying the endpoint cannot reach the report.
    """
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False, "DNS resolution failed"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, OK
    except OSError:
        return False, "TCP connect failed"


def _probe_agent_proxy(report, settings, env: Mapping[str, str] | None = None) -> None:
    """Report the Agent proxy policy with no endpoint material whatsoever.

    The endpoint is parsed and validated exactly as the product does, so the diagnosis matches what
    startup would do, but only the boolean decisions derived from it are reported. The parsed value
    stays in a local and never reaches a label, a detail or a log line.
    """
    from .agent_proxy import AgentProxyError, parse_agent_proxy

    proxy_settings = settings.agent.proxy if settings and settings.agent else None
    if proxy_settings is None or not proxy_settings.enabled:
        report.note("agent proxy", "disabled")
        return

    report.status("agent proxy", OK, "enabled")
    report.note(PROXY_SOURCE, proxy_settings.source)
    # A credential in the URL is refused at parse time, so a configured proxy is credentialless by
    # construction. Saying so is more useful than omitting it.
    report.note(PROXY_AUTH, "none")

    try:
        parsed = parse_agent_proxy(proxy_settings, env)
    except AgentProxyError as exc:
        # AgentProxyError messages are redacted by construction: they name the failure class.
        report.status(PROXY_BYPASS, FAIL, str(exc))
        report.status(PROXY_REACH, FAIL, "not evaluated (configuration refused)")
        return

    if parsed is None:
        report.note(PROXY_BYPASS, "not applicable (no endpoint)")
        report.status(PROXY_REACH, WARN, "no endpoint configured")
        return

    # The mandatory local bypass is a static property of the merged value, so it needs no network.
    required = {"127.0.0.1", "localhost", "::1"}
    present = {part.strip() for part in parsed.no_proxy.split(",") if part.strip()}
    report.status(PROXY_BYPASS, OK if required <= present else FAIL, "")

    host, port = _endpoint_parts(parsed.url)
    if host is None or port is None:
        report.status(PROXY_REACH, WARN, "endpoint shape not measurable")
        return
    ok, state = _reachable(host, port)
    report.status(PROXY_REACH, OK if ok else WARN, "" if ok else state)


def _endpoint_parts(url: str) -> tuple[str | None, int | None]:
    """Host and port for a reachability probe. Never returned to a report line."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, None
    if not parts.hostname:
        return None, None
    try:
        return parts.hostname, parts.port
    except ValueError:
        return None, None


def _probe_runtimes(report, settings) -> None:
    """Static per-runtime policy. No probe, no login, no inference (Phases E–F–G)."""
    if settings is None or settings.agent is None:
        return
    agent = settings.agent
    for name in ("codex", "claude", "qoder"):
        runtime = agent.runtime(name)
        if not runtime.enabled:
            report.note(f"agent {name}", "disabled")
            continue
        report.note(f"agent {name}", f"enabled, use_proxy={str(runtime.use_proxy).lower()}")


def _probe_data_home(report, env: Mapping[str, str] | None) -> None:
    """Whether the frozen native data home is determinable, without creating it.

    The data home is resolved from the *process* environment rather than the caller's override. That
    override exists so a test can supply a specific proxy endpoint, and letting it stand in for the
    whole environment would hide a real misconfiguration: a report that said "data home unavailable"
    because a caller passed a two-key proxy mapping would be reporting the test's shape, not the
    deployment's.
    """
    from .native_endpoint import data_home

    try:
        home = data_home()
    except NativeEndpointError as exc:
        report.status("agent data home", FAIL, str(exc))
        return
    report.status("agent data home", OK, "resolvable")

    lock_dir = home / "agent-bridge" / "locks"
    if lock_dir.exists():
        report.status("agent lock dir", OK, "present")
    else:
        # Not creating it is the point: doctor reports what is there, and the supervisor creates it.
        report.note("agent lock dir", "not created yet (the supervisor creates it on first start)")


def _probe_identity(report) -> None:
    """Whether this process can measure its own SID, which readiness will later compare against."""
    from .native_endpoint import current_user_sid
    from .windows_agent_pipe import BridgePipeError

    try:
        current_user_sid()
    except (BridgePipeError, OSError):
        report.status("agent identity", FAIL, "this process cannot read its own user SID")
        return
    # The SID is an identity rather than a secret, but it is still not useful in a report: what
    # the operator needs to know is whether it is measurable.
    report.status("agent identity", OK, "current user SID is measurable")


def _probe_endpoint(report, env: Mapping[str, str] | None) -> None:
    """The deterministic endpoint, and whether something is already serving it."""
    from .native_endpoint import current_user_sid

    try:
        endpoint = derive_pipe_name(current_user_sid())
    except (NativeEndpointError, OSError) as exc:
        report.status("agent endpoint", FAIL, type(exc).__name__)
        return
    report.status("agent endpoint", OK, "derivable from the current user identity")

    if not _pipe_is_served(endpoint):
        # The important wording: a static doctor must not become a launcher.
        report.note("agent bridge", "not running (start it with the native supervisor)")


def _pipe_is_served(endpoint: str, timeout: float = 1.0) -> bool:
    """Whether something is already listening on the Named Pipe.

    Connecting to a pipe that exists is only an existence observation; nothing is written to it,
    and the authenticated readiness probe lives in the MCP client. A doctor that issued a
    protocol call would be doing more than reporting.
    """
    if not sys.platform.startswith("win"):
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    # A zero timeout asks only whether the instance exists right now.
    return bool(kernel32.WaitNamedPipeW(endpoint, 0))


def _probe_bridge_availability(report, env: Mapping[str, str] | None) -> None:
    """Whether a Bridge distribution could be started at all.

    Availability only. Doctor never imports a second distribution into this process, because that
    would pull that code into the diagnostic path.
    """
    override = (env or {}).get("SERVERFS_BRIDGE_PYTHON", "").strip() or os.environ.get(
        "SERVERFS_BRIDGE_PYTHON", ""
    ).strip()
    candidate = Path(override) if override else None
    if candidate is not None:
        if candidate.is_file():
            report.status("agent bridge package", OK, "configured interpreter is present")
        else:
            report.status("agent bridge package", WARN, "configured interpreter is not present")
        return
    report.status(
        "agent bridge package",
        WARN,
        "not configured; set SERVERFS_BRIDGE_PYTHON or start through the native supervisor",
    )


def _probe_workdir_policy(report, workdirs) -> None:
    """Per-workdir Agent policy, as configured. No filesystem mutation."""
    for workdir in workdirs:
        if workdir.agent_mode == "disabled" and not workdir.agent_runtimes:
            continue
        runtimes = ",".join(sorted(workdir.agent_runtimes)) or "none"
        report.note(
            f"agent workdir {workdir.alias}",
            f"mode={workdir.agent_mode} runtimes={runtimes}",
        )


def report_agent(
    report,
    workdirs,
    settings,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """The Agent section of the report.

    A disabled or absent configuration produces exactly one informational line. That is
    deliberate: the default state is a supported configuration, and reporting it as a warning
    would make the default look broken.
    """
    if settings is None or settings.agent is None or not settings.agent.enabled:
        report.note("agent", "disabled")
        return

    report.status("agent", OK, "enabled")
    _probe_workdir_policy(report, workdirs)
    _probe_runtimes(report, settings)
    _probe_data_home(report, env)
    _probe_bridge_availability(report, env)
    _probe_identity(report)
    _probe_endpoint(report, env)
    _probe_agent_proxy(report, settings, env)


__all__ = [
    "PROXY_AUTH",
    "PROXY_BYPASS",
    "PROXY_ENABLED",
    "PROXY_REACH",
    "PROXY_SOURCE",
    "report_agent",
]
