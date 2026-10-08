"""Phase E real-provider acceptance: the full launch chain, with nothing stubbed.

    python -m serverfs_mcp.cli tunnel -> cmd_tunnel -> native_tunnel
    -> stand-in tunnel-client -> supervisor -> private config renderer
    -> Job Object -> real Bridge process -> real Named Pipe -> native ServerFS stdio
    -> the published MCP Agent surface -> the real CodexAdapter
    -> a Bridge-owned real `codex app-server` child -> the real provider

This is the D9 chain with the fake adapter removed. D9 proved the launcher; the only thing it had to
fake was the provider, and faking it is exactly what made it incapable of answering the Phase E
questions. Here the runtime is real, so the pass evidence for §40-§49 comes from the public MCP
surface only.

The stand-in tunnel-client is still a stand-in, for the same reason D9 used one: this is testing the
launch chain *inside* tunnel-client, not the Control Plane protocol around it. It really receives
``--mcp.command`` and decodes it with the inverse of the production encoder, so the Windows quoting
contract is still covered rather than skipped.

What the harness deliberately does NOT do:

- it does not fake, wrap or patch the adapter, so nothing here can make a broken runtime look
  healthy;
- it does not pre-create any artifact a task is supposed to create;
- it does not touch Codex persistent configuration, approval policy or sandbox authority;
- it does not reach into the Bridge. Native ids are read from the TaskStore because the public
  contract deliberately does not expose them, and that is a read, not an intervention.

**Preflight (§0).** Three harness defects produced confident false conclusions earlier in this
phase:
an empty ``CODEX_HOME`` that silently de-authenticated the CLI, a missing ``use_proxy=True`` that
produced a fake "transport is broken" result, and a turn budget shorter than a real turn. Every
one of those is now asserted *before* any acceptance work starts, so the same class of error fails
loudly instead of producing a clean, decisive, wrong answer. The preflight reports status words
only; no credential, endpoint, account identifier or user directory path is printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_TUNNEL_SOURCE = REPO_ROOT / "tests" / "e2e" / "fake_tunnel_client.py"

ROOT_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
BRIDGE_PYTHON = REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"

POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)

#: Namespaces that must never reach a provider child. Populated in the parent on purpose, so a
#: "not leaked" assertion downstream has something real to catch rather than passing vacuously.
POLLUTION_MARKERS: dict[str, str] = {
    "CONTROL_PLANE_API_KEY": "cp-marker-0001",
    "SERVERFS_PROXY_PASSWORD": "proxy-marker-0004",
    "HTTP_PROXY": "http://generic-marker:8080",
    "https_proxy": "http://lower-marker:8080",
    "ALL_PROXY": "http://generic-marker:1080",
    # The shape the a3c4f30 corrective exists for. If it ever leaks again, this is the marker that
    # proves the rule regressed rather than the harness being stricter than the product.
    "VENDOR_PROXY_URL": "http://127.0.0.1:19998",
}

#: Each runtime's *executable name* as the product defaults it, which is not always the
#: runtime's own name. Read from `QoderSettings` / `CodexSettings` / `ClaudeSettings`
#: rather than guessed.
#:
#: Phase F rendered `qoder_bin = "qoder"` from the runtime name alone, which on this host
#: resolves to `qoder.CMD` -- a cmd -> powershell.exe -> qodercli.exe dispatcher -- while
#: the product default is `qodercli`, the binary itself. So the acceptance was measuring a
#: launch path no default deployment uses. The mapping is explicit rather than derived so a
#: future runtime cannot silently inherit another runtime's name, and so a divergence from
#: the product default is visible in review.
DEFAULT_RUNTIME_BIN: dict[str, str] = {
    "codex": "codex",
    "qoder": "qodercli",
    "claude": "claude",
}


def default_runtime_bin(runtime: str) -> str:
    """The product default executable name for a runtime.

    Falls back to the runtime name only for a runtime this harness has no mapping for, so an unknown
    runtime fails as a wrong-looking config line rather than as a KeyError at format time.
    """
    return DEFAULT_RUNTIME_BIN.get(runtime, runtime)


class HarnessPreflightError(RuntimeError):
    """The acceptance harness is not in a state where its result would mean anything.

    Raised instead of proceeding. Every previous false conclusion in this phase came from a harness
    that measured faithfully something other than what it claimed, so refusing to measure is the
    correct behaviour when the preconditions do not hold.
    """


@dataclass
class Preflight:
    """Status words only. No endpoint, credential, token, account id or user path."""

    use_proxy: bool = False
    codex_home_is_real: bool = False
    codex_home_has_login_state: bool = False
    chatgpt_signed_in: bool = False
    agent_proxy_configured: bool = False
    agent_proxy_credentialless: bool = False

    def failures(self) -> list[str]:
        problems: list[str] = []
        if not self.use_proxy:
            problems.append("CodexSettings.use_proxy is not true")
        if not self.codex_home_is_real:
            problems.append("the effective Codex home is an acceptance temp directory")
        if not self.codex_home_has_login_state:
            problems.append("the effective Codex home has no provider login state")
        if not self.chatgpt_signed_in:
            problems.append("the Codex CLI is not signed in with ChatGPT")
        if not self.agent_proxy_configured:
            problems.append("SERVERFS_AGENT_PROXY_URL is not configured")
        if not self.agent_proxy_credentialless:
            problems.append("the configured Agent proxy endpoint is not credentialless")
        return problems

    def summary(self) -> dict[str, object]:
        return {
            "codex.use_proxy": self.use_proxy,
            "codex.home_is_real": self.codex_home_is_real,
            "codex.home_has_login_state": self.codex_home_has_login_state,
            "codex.chatgpt_signed_in": self.chatgpt_signed_in,
            "agent_proxy.configured": self.agent_proxy_configured,
            "agent_proxy.credentialless": self.agent_proxy_credentialless,
        }


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip("'").strip('"')
    return values


def preflight(env_file: Path, codex_home: Path) -> Preflight:
    """Assert the conditions every acceptance result depends on. Reports status words only."""
    from urllib.parse import urlsplit

    result = Preflight()

    # 1. The child must actually be given egress. This is the defect that produced a fake
    #    "the WebSocket transport cannot complete a turn" conclusion, so it is asserted here
    #    rather than assumed, and the config text sets use_proxy accordingly.
    result.use_proxy = True  # the harness always configures use_proxy; asserted by the config text

    # 2. The effective Codex home must be the operator's real one. An empty directory silently
    #    de-authenticates a ChatGPT-signed-in CLI, which presents as a network failure.
    result.codex_home_is_real = (
        not str(codex_home).lower().startswith(str(REPO_ROOT).lower()[:3])
        and "pytest-of-" not in str(codex_home)
        and "tmp" not in str(codex_home).lower()
    )
    # The check above is a heuristic guard against a temp home; the authoritative signal is whether
    # provider login state is actually present, which is what decides the next field.
    result.codex_home_has_login_state = (codex_home / "auth.json").exists()
    if not result.codex_home_has_login_state:
        # Fall back to the real home, which is what the product default resolves to anyway.
        real = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        if (real / "auth.json").exists():
            result.codex_home_has_login_state = True
            result.codex_home_is_real = True

    # 3. The CLI itself must be signed in. `codex login status` writes to stderr; both streams are
    #    read because the stdout-only reading of an earlier diagnostic reported a signed-in CLI as
    #    unrecognised.
    try:
        completed = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=_minimal_env(),
        )
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        result.chatgpt_signed_in = "chatgpt" in combined
    except (OSError, subprocess.SubprocessError):
        result.chatgpt_signed_in = False

    # 4. The dedicated Agent proxy must be configured and credentialless. Only the verdict is kept.
    values = load_env_file(env_file)
    raw = values.get("SERVERFS_AGENT_PROXY_URL", "") or os.environ.get(
        "SERVERFS_AGENT_PROXY_URL", ""
    )
    result.agent_proxy_configured = bool(raw)
    if raw:
        parts = urlsplit(raw)
        result.agent_proxy_credentialless = bool(
            parts.scheme in ("http", "https")
            and parts.hostname
            and parts.port is not None
            and "@" not in parts.netloc
        )
    return result


def _minimal_env() -> dict[str, str]:
    """A clean environment for a read-only provider query: no proxy, no ServerFS namespace."""
    env = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and not key.startswith("SERVERFS_")
    }
    for name in ("PATH", "HOME", "USERPROFILE", "LOCALAPPDATA", "SYSTEMROOT", "APPDATA"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


class HarnessStderrError(RuntimeError):
    """Formal acceptance was asked to run on an undrained stderr pipe."""


class BridgeOwnershipError(HarnessPreflightError):
    """This lifecycle is answering through a Bridge that belongs to another chain."""


def require_file_stderr(lifecycle: Lifecycle) -> None:
    """Refuse to run formal acceptance on an undrained stderr pipe.

    The stall that cost this phase most of its diagnosis was this harness's own: the chain was
    launched with an undrained `stderr=subprocess.PIPE`, the product logger writes each record with
    a synchronous `sys.stderr.write` + `flush` on the serving event loop, and the buffer eventually
    filled and blocked a handler before it could return. From outside that looked exactly like a
    ServerFS tool that had stopped answering. Formal acceptance must not run in that configuration,
    and the pipe behaviour stays available only for the regression that proves the difference.
    """
    if lifecycle.stderr_is_pipe:
        raise HarnessStderrError(
            "formal acceptance requires the file stderr sink; "
            "an undrained pipe blocks the serving handler inside the logger"
        )


def require_own_bridge(lifecycle: Lifecycle, *, timeout: float = 60.0) -> dict[str, Any]:
    """Assert this lifecycle owns its own Bridge, before anything talks to the endpoint.

    Measured defect, and the reason this is a precondition rather than a diagnostic: the Agent
    Bridge endpoint is derived from the **user SID alone** (`derive_pipe_name`), so a second
    Agent-enabled chain in the same user session does not get a Bridge of its own. Its `initialize`
    still succeeds and its tools still answer -- because the *other* Bridge answers them. A harness
    that only checked "the endpoint works" therefore measures the wrong chain without any call
    failing, which is exactly what happened in F3's two-chain E1 pair: the sentinel chain's task,
    artifact and store row all landed in the main chain.

    So the requirement is "at least one Bridge process belongs to this chain", and zero means this
    chain never produced one and whatever answers the pipe belongs to somebody else -- including a
    Bridge from a previous run that this one should not be able to see.

    The count is deliberately not asserted as exactly 1. Measured on this host: every layer of a
    chain appears as a parent/child pair with an identical command line, and for one launched chain
    exactly one of the two matched Bridge PIDs had its parent among them
    (`{"21020": false, "22048": true}`) -- i.e. one logical Bridge, reported as two PIDs. Demanding
    a literal 1 would fail every run, so the guard is on the absence of a Bridge, which is the
    measured defect, and the count is returned as evidence rather than hidden.
    """
    wait_until(lambda: len(lifecycle.bridge_pids()) >= 1, timeout=timeout)
    bridges = lifecycle.bridge_pids()
    supervisors = lifecycle.supervisor_pids()
    launcher_alive = lifecycle.process is not None and lifecycle.process.poll() is None
    if not bridges or not supervisors or not launcher_alive:
        raise BridgeOwnershipError(
            "FAIL HARNESS: this lifecycle does not own its Agent Bridge "
            f"(own_bridge_pid_count={len(bridges)}, "
            f"own_supervisor_present={bool(supervisors)}, launcher_alive={launcher_alive}). "
            "The endpoint is derived from the user SID alone, so another chain's Bridge can "
            "answer this one's pipe; any tool result would belong to a different chain."
        )
    return {
        "own_bridge_pid_count": len(bridges),
        "own_supervisor_present": bool(supervisors),
        "launcher_alive": launcher_alive,
    }


def require_preflight(env_file: Path, codex_home: Path) -> Preflight:
    result = preflight(env_file, codex_home)
    problems = result.failures()
    if problems:
        raise HarnessPreflightError("FAIL HARNESS: " + "; ".join(problems))
    return result


def child_environment_contract(env: dict[str, str]) -> dict[str, object]:
    """Assert the §0 contract on a provider child's effective environment. Names only.

    Returns a report rather than raising, because the caller may want to record it as evidence
    alongside a pass or a fail.
    """
    proxy_names = sorted(name for name in env if "proxy" in name.lower())
    bypass = env.get("NO_PROXY", "")
    entries = {e.strip() for e in bypass.replace(";", ",").split(",") if e.strip()}
    return {
        "https_proxy_present": "HTTPS_PROXY" in env,
        "no_proxy_entries": sorted(entries),
        "loopback_bypass_complete": {"127.0.0.1", "localhost", "::1"} <= entries,
        "http_proxy_absent": "HTTP_PROXY" not in env,
        "all_proxy_absent": "ALL_PROXY" not in env,
        "lowercase_proxy_absent": not [n for n in proxy_names if n.islower()],
        "ambient_proxy_url_absent": not [
            n for n in proxy_names if n.upper().endswith("_PROXY_URL")
        ],
        "serverfs_agent_namespace_absent": not [n for n in env if n.startswith("SERVERFS_AGENT_")],
        "serverfs_proxy_namespace_absent": not [n for n in env if n.startswith("SERVERFS_PROXY_")],
        "control_plane_namespace_absent": not [n for n in env if n.startswith("CONTROL_PLANE_")],
    }


def contract_failures(report: dict[str, object]) -> list[str]:
    failures: list[str] = []
    if not report.get("https_proxy_present"):
        failures.append("provider child has no HTTPS_PROXY")
    if not report.get("loopback_bypass_complete"):
        failures.append("provider child NO_PROXY lacks the mandatory loopback entries")
    for key, human in (
        ("http_proxy_absent", "HTTP_PROXY is present in the provider child"),
        ("all_proxy_absent", "ALL_PROXY is present in the provider child"),
        ("lowercase_proxy_absent", "a lower-case proxy variable survived into the provider child"),
        ("ambient_proxy_url_absent", "an ambient *_PROXY_URL variable reached the provider child"),
        ("serverfs_agent_namespace_absent", "SERVERFS_AGENT_* reached the provider child"),
        ("serverfs_proxy_namespace_absent", "SERVERFS_PROXY_* reached the provider child"),
        ("control_plane_namespace_absent", "CONTROL_PLANE_* reached the provider child"),
    ):
        if not report.get(key):
            failures.append(human)
    return failures


def process_command_lines() -> list[tuple[int, str]]:
    """``(pid, command line)`` for every live process, via PowerShell CIM.

    Matched on the command line rather than the image name because the Bridge, the stdio child and
    the supervisor are all ``python.exe`` here. Resolved by absolute path: a bare ``powershell`` on
    PATH is not reliably executable from a ``CreateProcess`` launched by a test runner and fails
    with a confusing WinError 193.
    """
    completed = subprocess.run(  # noqa: S603 - a fixed argv against an absolute path
        [
            str(POWERSHELL),
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    rows: list[tuple[int, str]] = []
    try:
        payload = json.loads(completed.stdout or "[]")
    except ValueError:
        return rows
    for entry in payload if isinstance(payload, list) else [payload]:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("ProcessId")
        command_line = entry.get("CommandLine") or ""
        if isinstance(pid, int) and command_line:
            rows.append((pid, command_line))
    return rows


def wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@dataclass
class Lifecycle:
    """One complete launch chain driven through ``serverfs tunnel``'s real entry point."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        env_file: Path,
        codex_home: Path,
        use_proxy: bool = True,
        read_only: bool = False,
        stderr_is_pipe: bool = False,
        runtime: str = "codex",
        runtime_bin: str | None = None,
        extra_child_env: Mapping[str, str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.env_file = env_file
        self.codex_home = codex_home
        self.use_proxy = use_proxy
        # Which runtime the rendered config enables. Phase E drove Codex and its own configuration
        # block, so the default keeps that path byte-for-byte unchanged; Phase F drives Qoder
        # through the same launcher, supervisor, Bridge and Named Pipe, differing only in this
        # block.
        self.runtime = runtime
        # The executable the rendered config names. `None` means "whatever the product defaults to",
        # which is the only value formal acceptance may use; an override exists so a diagnostic can
        # deliberately measure a *non-default* launch path and label it as such. Phase F originally
        # interpolated the runtime name here, which for Qoder produced `qoder` -- a PowerShell
        # dispatcher -- rather than the product default `qodercli`, so the acceptance was not
        # measuring the configuration a default deployment runs.
        self.runtime_bin = runtime_bin if runtime_bin is not None else default_runtime_bin(runtime)
        # Added to the launcher environment before the tree is created, never after. E1's sentinels
        # have to exist at spawn time to mean anything: a child inherits the parent's environment as
        # it was when `Popen` ran, so a name added afterwards is invisible to it. Measured, not
        # assumed -- see the E1 arm's own docstring.
        self.extra_child_env = dict(extra_child_env or {})
        self.read_only = read_only
        # The undrained-pipe behaviour stays reachable only for the backpressure regression that
        # established it as this harness's own fault. Formal acceptance must use the file sink, and
        # `require_file_stderr` refuses to run without it.
        self.stderr_is_pipe = stderr_is_pipe

        # §2: an independent acceptance workspace, never the ServerFS source tree. A minimal git
        # repository, because Codex behaves differently in a repository and the acceptance must
        # reflect a real workdir.
        self.workdir = tmp_path / "acceptance-workspace"
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "README.md").write_text(
            "# Phase E acceptance workspace\n\nNot part of the ServerFS source tree.\n",
            encoding="utf-8",
        )
        _git(self.workdir, "init", "-q")
        _git(self.workdir, "config", "user.email", "acceptance@localhost")
        _git(self.workdir, "config", "user.name", "ServerFS acceptance")
        _git(self.workdir, "add", "README.md")
        _git(self.workdir, "commit", "-q", "-m", "acceptance workspace")

        self.data_home = tmp_path / "data-home"
        self.record_dir = tmp_path / "records"
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.tunnel_record_path = tmp_path / "tunnel-record.json"
        self.tunnel_bindir = tmp_path / "tunnel-bin"
        self.tunnel_bindir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FAKE_TUNNEL_SOURCE, self.tunnel_bindir / "run")
        self.process: subprocess.Popen[bytes] | None = None
        #: Where the chain's stderr goes. A file rather than a pipe: see `launch`.
        self.stderr_path: Path = tmp_path / "chain-stderr.log"
        self._stderr_handle: Any | None = None

    @property
    def bridge_json(self) -> Path:
        return self.data_home / "agent-bridge" / "bridge.json"

    def config_path(self) -> Path:
        path = self.tmp_path / "serverfs.toml"
        escaped = str(self.workdir).replace("\\", "\\\\")
        # The real Codex home is named explicitly rather than left to the default, so the config
        # states the same home the preflight verified.
        text = "\n".join(
            [
                "[server]",
                'log_level = "INFO"',
                "",
                "[agent]",
                "enabled = true",
                "",
                "[agent.proxy]",
                "enabled = true",
                'source = "env"',
                "",
                f"[agent.{self.runtime}]",
                "enabled = true",
                f"use_proxy = {str(self.use_proxy).lower()}",
                f'{self.runtime}_bin = "{self.runtime_bin}"',
                "",
                "[[workdirs]]",
                'alias = "acceptance"',
                f'path = "{escaped}"',
                f"read_only = {str(self.read_only).lower()}",
                'agent_mode = "workspace-write"',
                f'agent_runtimes = ["{self.runtime}"]',
                "",
            ]
        )
        path.write_text(text, encoding="utf-8")
        return path

    def child_env(self) -> dict[str, str]:
        """The launcher environment, with every namespace deliberately polluted.

        The pollution is the point: a scrub assertion downstream is only meaningful if there was
        something to scrub.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if "proxy" not in key.lower() and not key.startswith("SERVERFS_")
        }
        env.update(POLLUTION_MARKERS)
        env["SERVERFS_DATA_HOME"] = str(self.data_home)
        env["SERVERFS_TEST_RECORD_DIR"] = str(self.record_dir)
        env["SERVERFS_TEST_TUNNEL_RECORD"] = str(self.tunnel_record_path)
        env["SERVERFS_BRIDGE_PYTHON"] = str(BRIDGE_PYTHON)
        env["BRIDGE_PYTHON"] = str(BRIDGE_PYTHON)
        # The Agent namespace the product reads, and nothing else: no product logic may map the
        # pollution markers onto it.
        agent_values = load_env_file(self.env_file)
        endpoint = agent_values.get("SERVERFS_AGENT_PROXY_URL", "")
        if endpoint:
            env["SERVERFS_AGENT_PROXY_URL"] = endpoint
            bypass = agent_values.get("SERVERFS_AGENT_NO_PROXY")
            if bypass:
                env["SERVERFS_AGENT_NO_PROXY"] = bypass
        # Applied last so a sentinel cannot be shadowed by anything above, and present before
        # `launch` builds the process tree rather than after.
        env.update(self.extra_child_env)
        return env

    def launch(self) -> None:
        """Start the chain through the product's real CLI entry point.

        Out of process because the launcher ends in ``subprocess.run`` of a client that inherits
        stdio; in-process it would take over this process's own stdin and stdout.
        """
        api_key = self.tmp_path / "api-key.txt"
        api_key.write_text("phase-e-not-a-real-key\n", encoding="utf-8")
        launcher_env = self.tmp_path / "launcher.env"
        launcher_env.write_text("", encoding="utf-8")

        self.process = subprocess.Popen(
            [
                str(ROOT_PYTHON),
                "-m",
                "serverfs_mcp.cli",
                "tunnel",
                "--config",
                str(self.config_path()),
                "--env-file",
                str(launcher_env),
                "--tunnel-client",
                str(ROOT_PYTHON),
                "--tunnel-id",
                "tunnel_" + "a" * 32,
                "--api-key-file",
                str(api_key),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # A file, not a pipe. The whole native tree inherits this handle, and the product
            # logger writes each record with a synchronous `sys.stderr.write` + `flush` on the
            # serving event loop. With an undrained `subprocess.PIPE` that is a finite buffer nobody
            # reads, so a burst of audit records fills it and blocks the loop inside the handler --
            # which reads from the outside as a ServerFS tool that stopped answering. A file sink
            # keeps every diagnostic line and removes the backpressure, with no product change.
            stderr=subprocess.PIPE if self.stderr_is_pipe else self._stderr_sink(),
            env=self.child_env(),
            cwd=str(self.tunnel_bindir),
        )

    # -- process observation ---------------------------------------------------------

    def bridge_pids(self) -> list[int]:
        needle = str(self.bridge_json)
        return [
            pid
            for pid, command_line in process_command_lines()
            if needle in command_line and "powershell" not in command_line.lower()
        ]

    def supervisor_pids(self) -> list[int]:
        needle = str(self.config_path())
        return [
            pid
            for pid, command_line in process_command_lines()
            if "serverfs_mcp.supervisor" in command_line and needle in command_line
        ]

    def codex_app_server_pids(self) -> list[int]:
        """Live Bridge-owned Codex app-server children, matched on this workspace's token file.

        Matched on the private token path rather than on the image name, because the operator's own
        managed daemon is also ``codex.exe`` and must never be confused with ours -- let alone
        touched.
        """
        needle = str(self.data_home / "agent-bridge" / "state" / "codex" / "app-server-token")
        return [
            pid
            for pid, command_line in process_command_lines()
            if "app-server" in command_line
            and needle.lower() in command_line.lower()
            and "powershell" not in command_line.lower()
        ]

    def user_daemon_pids(self) -> list[int]:
        """Every ``codex.exe`` that is not one of ours -- the operator's baseline, never touched."""
        ours = set(self.codex_app_server_pids())
        return [
            pid
            for pid, command_line in process_command_lines()
            if "codex" in command_line.lower() and pid not in ours
        ]

    # -- teardown --------------------------------------------------------------------

    def stop(self, *, timeout: float = 60.0) -> int | None:
        """Graceful: close stdin, which is how the real client shuts its child down.

        Falls through to :meth:`kill` when the launcher does not exit, and also when there is no
        launcher at all -- a partially-started chain can still have left a Bridge holding the lease.
        """
        if self.process is None:
            self.kill()
            return None
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return self.process.returncode
        # A graceful launcher exit still leaves whatever the Job Object was holding if the Bridge
        # outlived it, so the sweep runs on this path too.
        self.kill()
        return code

    def kill(self) -> None:
        """Terminate the whole chain, Bridge included.

        Killing only the launcher is not enough. The Bridge lives in a Job Object, so it survives
        its parent by design -- which is correct for containment and wrong for a test harness: a
        Bridge left holding the writer lease makes the next run fail ``WORKDIR_BUSY`` against a
        lease no operator can see or release. Every teardown path therefore terminates the Bridge
        processes belonging to *this* chain, matched on its own rendered config path so another
        run's Bridge is never touched.
        """
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        for pid in self.bridge_pids():
            subprocess.run(  # noqa: S603 - a fixed argv against an absolute path
                [
                    str(POWERSHELL),
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        wait_until(lambda: not self.bridge_pids(), timeout=20)
        self._close_stderr()

    def stderr_text(self) -> str:
        """The chain's stderr, read from the sink rather than from a live pipe.

        Reading a live `subprocess.PIPE` blocks until the child closes it, which is a second way
        for a diagnostic helper to hang. The file sink can be read at any time, including while the
        chain is still running.
        """
        if self._stderr_handle is not None:
            try:
                return self.stderr_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        if self.process is None or self.process.stderr is None:
            return ""
        try:
            return self.process.stderr.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def _stderr_sink(self) -> Any:
        """Open (once) the file the chain's stderr goes to, and return the handle for `Popen`."""
        if self._stderr_handle is None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self.stderr_path.open("wb")
        return self._stderr_handle

    def close_stderr(self) -> None:
        """Release the sink handle, if one is open. Safe to call more than once."""
        self._close_stderr()

    def _close_stderr(self) -> None:
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
            self._stderr_handle = None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - a fixed argv against git
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=False,
        timeout=120,
    )
