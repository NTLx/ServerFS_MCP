"""Agent-aware diagnostics for ``serverfs doctor`` (v0.11 Phase D, §13, §15 D8).

Rules this module must never break, all of which tighten the v0.10 Tunnel contract rather than
relaxing it:

- **read-only.** Every check here inspects configuration, derives a path, or opens a socket for a
  reachability probe. Nothing creates the Agent data tree, renders a Bridge config, writes a lease,
  or starts a Bridge or a provider. A diagnostic that made the deployment startable would be a
  launcher wearing a report's clothes.
- **never a launcher.** If the Bridge is not running, doctor says so and says the supervisor owns
  starting it. When it *is* running, doctor may make a read-only existence observation, because
  that is an observation of an existing process, not a decision to create one.
- **stdout stays empty.** Lines go to the writer (stderr in the CLI), because stdout is reserved for
  MCP frames (§27).
- **the endpoint never appears.** Phase 0F §8 measured that a provider's own health report
  can report reachability without its endpoint, and that is the shape used here. The permitted
  vocabulary is enabled/disabled, source, authentication, local bypass and reachability. Host,
  port, URL, userinfo, credential and the raw environment value are all absent, including from
  exception text, which is why the reachability probe normalizes failures into a fixed vocabulary.

A disabled Agent is a normal configuration, so it is reported as a *note* and never as WARN or
FAIL. A doctor that cried wolf about the default state would train operators to ignore it.

**Two subprocesses, both bounded and both scrubbed.** The Bridge-package availability check and the
private-state inspection each run a child under the interpreter the supervisor would use, because
both questions are about what that interpreter can see rather than about what this process can see.
Neither child is given the Agent proxy endpoint or any Tunnel or Control Plane credential, and
neither is allowed to start a Bridge: the package probe asks ``importlib`` for a spec without
importing anything, and the state inspector only reads descriptors.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .native_endpoint import NativeEndpointError, data_home, derive_pipe_name

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

#: The same variable the supervisor reads, named here so the two cannot drift apart silently.
BRIDGE_PYTHON_ENV = "SERVERFS_BRIDGE_PYTHON"

#: A diagnostic must not hang because an interpreter did. Long enough for a cold Windows start of a
#: pure-Python import check, short enough that a wedged child is reported rather than waited on.
BRIDGE_PROBE_TIMEOUT_SECONDS = 20.0

#: The private-state inspector lives in the Bridge package (§23/§70 keep the two independent), so
#: doctor reaches it through a bounded child rather than reimplementing the ACL contract. A copy
#: would be a second answer that could disagree with the first, which is the failure Phase B
#: already demonstrated with the lease identity.
INSPECT_TIMEOUT_SECONDS = 20.0

#: Codex CLI diagnostics (Phase E §35). Bounded like every other child here: `--version` and
#: `--help` are pure reads, and a wedged CLI must be reported rather than waited on.
CODEX_PROBE_TIMEOUT_SECONDS = 20.0

#: The official app-server options the Windows transport is built on. Their absence is protocol
#: drift against a CLI that has moved on, and it has to be visible in a diagnostic rather than
#: surfacing later as an opaque "runtime not ready".
CODEX_REQUIRED_FLAGS = ("--listen", "--ws-auth", "--ws-token-file")

#: Report labels for the Codex checks. Separate from the proxy vocabulary above: these carry a
#: version string and a fixed auth vocabulary, and neither is an endpoint.
CODEX_CLI = "codex cli"
CODEX_APP_SERVER_FLAGS = "codex app-server flags"
CODEX_AUTH = "codex authentication"


def _lookup(env: Mapping[str, str] | None, name: str) -> str:
    """One environment value, preferring the caller's narrowing override over the process value.

    ``env`` is a narrowing device for the proxy probe, not a replacement environment, so a missing
    key falls through to the process rather than reading as empty. An earlier version treated ``{}``
    as "an environment with nothing in it", which made a real misconfiguration indistinguishable
    from a deliberate narrowing.
    """
    if env is not None and name in env:
        return env[name]
    return os.environ.get(name, "")


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

    try:
        parsed = parse_agent_proxy(proxy_settings, env)
    except AgentProxyError as exc:
        # AgentProxyError messages are redacted by construction: they name the failure class.
        # The authentication line is deliberately *not* printed before this point -- claiming
        # "authentication: none" about an endpoint that was refused would be a contradiction in a
        # diagnostic, and a reader has no way to tell which half of it to believe.
        report.status(PROXY_BYPASS, FAIL, str(exc))
        report.status(PROXY_REACH, FAIL, "not evaluated (configuration refused)")
        return

    # Only after a successful parse is the endpoint credentialless by construction, so only now is
    # "authentication: none" a statement about something that was actually inspected.
    report.note(PROXY_AUTH, "none")

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
    """Static per-runtime policy, plus read-only Codex diagnostics when Codex is enabled.

    The static part is unchanged and applies to all three runtimes: enabled/disabled and
    ``use_proxy``, nothing more. Only Codex gains an executable probe, because on Windows the
    Bridge owns a ``codex app-server`` child and an operator's first question is whether the CLI
    they have is one this runtime can actually drive.

    Nothing here starts an app-server, runs a turn, or touches provider state. The three checks are
    ``--version``, whether the official app-server flags this runtime depends on are still offered,
    and ``codex login status`` -- which the CLI documents as "Show login status" and which prints
    only the authentication method. Its raw output is never reported: it is reduced to a fixed
    vocabulary here, so a future CLI that prints an account identifier cannot leak one into a
    report line.
    """
    if settings is None or settings.agent is None:
        return
    agent = settings.agent
    for name in ("codex", "claude", "qoder"):
        runtime = agent.runtime(name)
        if not runtime.enabled:
            report.note(f"agent {name}", "disabled")
            continue
        report.note(f"agent {name}", f"enabled, use_proxy={str(runtime.use_proxy).lower()}")
        if name == "codex":
            _probe_codex_cli(report, runtime)


def _codex_argv(binary: str, *args: str) -> list[str]:
    return [binary, *args]


def _run_codex(binary: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run one read-only Codex command, bounded and scrubbed. ``None`` if it could not run.

    The child gets ``bridge_environment(None)``, the same scrub the other probes use, so a doctor
    run cannot hand the Codex CLI an Agent proxy endpoint or any Tunnel/Control Plane credential
    while diagnosing the proxy.
    """
    from .agent_proxy import bridge_environment

    try:
        return subprocess.run(  # noqa: S603 - argv is the configured binary plus fixed literals
            _codex_argv(binary, *args),
            capture_output=True,
            text=True,
            timeout=CODEX_PROBE_TIMEOUT_SECONDS,
            env=bridge_environment(None),
            cwd=str(Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_codex_cli(report, runtime) -> None:
    """Bounded, read-only, redacted checks of the local Codex CLI."""
    binary = runtime.codex_bin
    version = _run_codex(binary, "--version")
    if version is None:
        report.status(CODEX_CLI, WARN, "not executable from this environment")
        return
    if version.returncode != 0:
        report.status(CODEX_CLI, WARN, "did not report a version")
        return
    # The version string is not a secret, and it is the first thing an operator needs when a
    # runtime refuses to start, so it is reported verbatim.
    reported = version.stdout.strip().splitlines()
    report.status(CODEX_CLI, OK, reported[0] if reported else "")

    # The flags this runtime is built on. Their absence is protocol drift, and it must be visible
    # here rather than as an opaque startup failure later.
    help_text = _run_codex(binary, "app-server", "--help")
    if help_text is None or help_text.returncode != 0:
        report.status(CODEX_APP_SERVER_FLAGS, WARN, "app-server options not readable")
    else:
        missing = [flag for flag in CODEX_REQUIRED_FLAGS if flag not in help_text.stdout]
        if missing:
            report.status(
                CODEX_APP_SERVER_FLAGS,
                FAIL,
                "this Codex CLI does not offer the flags the Windows runtime requires",
            )
        else:
            report.status(CODEX_APP_SERVER_FLAGS, OK, "loopback listener and token flags available")

    status = _run_codex(binary, "login", "status")
    if status is None or status.returncode != 0:
        report.status(CODEX_AUTH, WARN, "login status not readable")
        return
    # Reduced to a fixed vocabulary. The raw line is discarded, never reported.
    #
    # Both streams are read because this CLI writes the status to stderr: measured on
    # codex-cli 0.159.2, `codex login status` leaves stdout empty. Reading stdout alone would
    # report every correctly signed-in deployment as unrecognised, which is the kind of false
    # negative that teaches operators to ignore the line.
    combined = f"{status.stdout}\n{status.stderr}".lower()
    if "chatgpt" in combined:
        report.status(CODEX_AUTH, OK, "signed in with ChatGPT")
    elif "api key" in combined or "apikey" in combined:
        report.status(CODEX_AUTH, OK, "signed in with an API key")
    elif "not logged in" in combined or "not signed in" in combined:
        report.status(CODEX_AUTH, WARN, "not signed in")
    else:
        report.status(CODEX_AUTH, WARN, "sign-in state not recognised")


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
        data_home()
    except NativeEndpointError as exc:
        report.status("agent data home", FAIL, str(exc))
        return
    report.status("agent data home", OK, "resolvable")
    _probe_private_state(report, env)


def _probe_private_state(report, env: Mapping[str, str] | None) -> None:
    """Report whether the private state on disk is safe, via the Bridge's own inspector.

    "Is the data home derivable" is a different question from "is the state that is already there
    safe", and only the second one is the private-state safety check the D8 contract asks for. A
    reparse point, a wrong object type, a foreign owner or a broad DACL under the Agent data home
    all have to be visible here, because each of them is an object the Bridge would refuse to use at
    startup -- and a diagnostic that cannot see them is not diagnosing anything.

    The judgement is delegated rather than reimplemented. ``serverfs_mcp`` owns no ACL knowledge and
    must not acquire any: §23/§70 freeze the packages as independent, and a second copy of the DACL
    rules could disagree with the Bridge's, at which point doctor would reassure an operator about a
    deployment the Bridge refuses to start. So the inspector runs in a child under the same
    interpreter, returns four bounded statuses, and only those statuses are reported.
    """
    candidate = _bridge_interpreter(env)
    home = _data_home_or_none(env)
    if home is None:
        report.status("agent private state", FAIL, "the Agent data home is not derivable here")
        return

    payload = _run_state_inspector(candidate, home)
    if payload is None:
        report.status(
            "agent private state",
            WARN,
            "the private-state inspector could not run; safety was not established either way",
        )
        return

    # An unrecognised status is treated as unknown rather than ignored. A renamed or extended
    # vocabulary on the Bridge side must not silently become "nothing was wrong here".
    unsafe = [key for key, value in payload.items() if value.get("status") == UNSAFE_STATUS]
    unknown = [
        key
        for key, value in payload.items()
        if value.get("status") not in {SAFE_STATUS, ABSENT_STATUS, UNSAFE_STATUS}
    ]
    if unsafe:
        # Naming which locations are wrong is the useful part; the reasons come back already
        # normalized by the inspector and contain no path, SID or descriptor detail.
        detail = ", ".join(
            f"{key} {payload[key].get('reason', UNSAFE_STATUS)}" for key in sorted(unsafe)
        )
        report.status("agent private state", FAIL, detail)
        return
    if unknown:
        keys = ", ".join(sorted(unknown))
        report.status("agent private state", WARN, f"could not be established for: {keys}")
        return
    absent = sum(1 for value in payload.values() if value.get("status") == ABSENT_STATUS)
    if absent:
        report.status(
            "agent private state",
            OK,
            f"safe where present ({absent} of {len(payload)} not created yet)",
        )
        return
    report.status("agent private state", OK, "all locations are present and private")


#: The inspector's vocabulary, restated so a rename on the Bridge side is caught here rather than
#: silently turning every location into "could not be established".
ABSENT_STATUS = "absent"
SAFE_STATUS = "safe"
UNSAFE_STATUS = "unsafe"
UNKNOWN_STATUS = "unknown"


def _data_home_or_none(env: Mapping[str, str] | None) -> Path | None:
    try:
        return data_home()
    except NativeEndpointError:
        return None


def _run_state_inspector(candidate: Path, home: Path) -> dict[str, dict[str, str]] | None:
    """Run the Bridge's read-only inspector and return its report. ``None`` if it could not run."""
    from .agent_proxy import bridge_environment

    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argv against a resolved interpreter
            [
                str(candidate),
                "-m",
                "serverfs_agent_bridge.inspect_state",
                "--data-home",
                str(home),
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_SECONDS,
            env=bridge_environment(None),
            cwd=str(Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        key: value
        for key, value in payload.items()
        if isinstance(value, dict) and isinstance(value.get("status"), str)
    }


def _probe_identity(report) -> None:
    """Whether this process can measure the identity the Bridge will assert."""
    if sys.platform == "win32":
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
        return
    # POSIX (Linux / macOS v0.13): the AF_UNIX Bridge authorizes the peer by
    # the kernel-measured uid/gid (SO_PEERCRED / getpeereid) — never a
    # fabricated PID — so the measurable fact is this process's uid.
    report.status("agent identity", OK, "AF_UNIX peer identity via uid/gid (measurable)")


def _probe_endpoint(report, env: Mapping[str, str] | None) -> None:
    """The deterministic endpoint, and whether something is already serving it."""
    if sys.platform == "darwin":
        # v0.13: the endpoint derives from the OS-provided per-user runtime
        # dir (§11 D2); probe presence with a read-only connect, mirroring
        # the Windows pipe observation's "existence, nothing more" rule.
        from .native_endpoint import derive_endpoint

        try:
            endpoint = derive_endpoint()
        except (RuntimeError, OSError) as exc:
            report.status("agent endpoint", FAIL, type(exc).__name__)
            return
        report.status("agent endpoint", OK, "derived from the per-user runtime directory")
        if _socket_is_served(endpoint):
            report.note(
                "agent bridge",
                "socket present (an AF_UNIX listener exists; identity and health are not "
                "established here)",
            )
        else:
            report.note(
                "agent bridge",
                "not running (start it with: serverfs agent-bridge start)",
            )
        return
    if sys.platform.startswith("linux"):
        # The Linux deployment supplies the endpoint explicitly (Phase E
        # configuration); derivation is a Windows/macOS contract.
        endpoint = (_lookup(env, "SERVERFS_AGENT_BRIDGE_SOCKET") or "").strip()
        if endpoint:
            report.status("agent endpoint", OK, "supplied by deployment configuration")
        else:
            report.note("agent endpoint", "not set (SERVERFS_AGENT_BRIDGE_SOCKET unset)")
        return
    if sys.platform == "win32":
        from .native_endpoint import current_user_sid

        try:
            endpoint = derive_pipe_name(current_user_sid())
        except (NativeEndpointError, OSError) as exc:
            report.status("agent endpoint", FAIL, type(exc).__name__)
            return
    report.status("agent endpoint", OK, "derivable from the current user identity")

    if _pipe_is_served(endpoint):
        # Precisely what was observed and nothing more. WaitNamedPipe answers "an instance of this
        # name exists right now"; it does not authenticate the peer, check the protocol version or
        # confirm the service is healthy. Calling this "ready" or "healthy" would be a claim the
        # probe did not support, and an operator who trusts it would skip the check that matters.
        report.note(
            "agent bridge",
            "pipe present (a Named Pipe instance exists; identity and health are not established "
            "here -- the supervisor's startup check verifies those)",
        )
    else:
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


def _socket_is_served(endpoint: str, timeout: float = 1.0) -> bool:
    """Whether something is listening on the AF_UNIX endpoint (existence only).

    The same observation rule as the Windows pipe probe: a successful
    connect proves a listener exists, nothing is written, and identity or
    health are the client's authenticated readiness check, not doctor's.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            probe.connect(endpoint)
        return True
    except OSError:
        return False


def _bridge_interpreter(env: Mapping[str, str] | None) -> Path:
    """The interpreter the supervisor would use, resolved exactly as the supervisor resolves it.

    ``SERVERFS_BRIDGE_PYTHON`` when set, otherwise ``sys.executable``. An earlier version warned
    whenever the variable was absent, which is a false positive on the common deployment: the MCP
    server and the Bridge frequently share one interpreter, so the supervisor needs no override and
    a diagnostic that says "not configured" would send an operator looking for a problem that is not
    there. The rule is restated here rather than imported, because the supervisor's constant is a
    private detail of a module this one must not depend on, and a copy that is tested against the
    real behaviour is safer than a stale import.
    """
    override = _lookup(env, BRIDGE_PYTHON_ENV).strip()
    return Path(override) if override else Path(sys.executable)


def _probe_bridge_availability(report, env: Mapping[str, str] | None) -> None:
    """Whether the Bridge could actually be started from this deployment.

    Availability, not configuration. The question an operator needs answered is "will the supervisor
    be able to launch the Bridge?", so the probe resolves the same interpreter the supervisor would
    and asks that interpreter whether the Bridge distribution is importable. Warning because a
    variable is unset answers a different, less useful question.

    The check runs in a bounded subprocess rather than in this process: importing a second
    distribution into the diagnostic path would pull that code into ``serverfs doctor``, and the
    subprocess also proves the answer for the *candidate* interpreter rather than for whichever one
    happens to be running the doctor. The child does one thing -- ask importlib for a module spec --
    and is given the strict Bridge scrub so it cannot see the Agent proxy endpoint or any Tunnel or
    Control Plane credential while doing it.
    """
    candidate = _bridge_interpreter(env)
    if not candidate.is_file():
        report.status(
            "agent bridge package",
            FAIL,
            "the interpreter the supervisor would use is not present on this machine",
        )
        return

    completed = _probe_bridge_package(candidate, env)
    if completed is None:
        report.status(
            "agent bridge package",
            FAIL,
            "the Bridge distribution could not be inspected with the configured interpreter",
        )
        return
    found, detail = completed
    if found:
        report.status(
            "agent bridge package",
            OK,
            "importable by the interpreter the supervisor would use"
            + (f" ({detail})" if detail else ""),
        )
    else:
        report.status(
            "agent bridge package",
            FAIL,
            "the Bridge distribution is not importable by the interpreter the supervisor would "
            "use, so agent delegation cannot start",
        )


#: The child asks importlib for a spec and prints a single token. Nothing is imported, no module
#: code runs, and no Bridge service is constructed -- a diagnostic must not start the thing it is
#: diagnosing.
_FIND_SPEC_SCRIPT = (
    "import importlib.util,sys\n"
    "spec = importlib.util.find_spec('serverfs_agent_bridge')\n"
    "print('FOUND' if spec is not None else 'MISSING')\n"
)


def _probe_bridge_package(
    candidate: Path, env: Mapping[str, str] | None
) -> tuple[bool, str] | None:
    """Ask one interpreter whether the Bridge distribution is importable. ``None`` on failure.

    Bounded on both axes: a wall-clock timeout, because a hung interpreter must not hang a
    diagnostic, and a scrubbed environment, because this process may hold the Agent proxy endpoint
    and Tunnel credentials that have no business reaching a child.
    """
    # The child is scrubbed like any Bridge child. A narrowing override is forwarded so a test can
    # redirect the probe, but it never reaches the child as a credential-bearing value: the scrub
    # runs after the merge.
    from .agent_proxy import bridge_environment

    merged: dict[str, str] | None = None
    if env:
        merged = {**os.environ, **env}
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed argv against a resolved interpreter
            [str(candidate), "-c", _FIND_SPEC_SCRIPT],
            capture_output=True,
            text=True,
            timeout=BRIDGE_PROBE_TIMEOUT_SECONDS,
            env=bridge_environment(merged),
            cwd=str(Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    answer = completed.stdout.strip()
    if answer == "FOUND":
        return True, ""
    if answer == "MISSING":
        return False, ""
    return None


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
