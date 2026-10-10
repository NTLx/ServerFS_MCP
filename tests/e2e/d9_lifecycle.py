"""D9 harness: the real Windows native launch chain, end to end, over real MCP stdio.

What is exercised, in order, with nothing stubbed between the steps:

    python -m serverfs_mcp.cli tunnel -> cmd_tunnel -> native_tunnel
    -> fake tunnel-client -> supervisor -> private config renderer
    -> Job Object -> real Bridge process -> real Named Pipe -> native ServerFS stdio
    -> the published MCP Agent surface -> the deterministic provider adapter

The chain starts at the product's own CLI entry point. An earlier version imported
``run_native_tunnel`` and called it directly, which skipped argparse, ``cmd_tunnel`` and the CLI's
error normalization -- one layer short of what an operator runs, and it made a redacted CLI failure
look like a traceback.

Only the provider adapter at the end is a test double, and it is a ``sitecustomize`` rather than a
repository change, so the production surface has no test-only flag. The public runtime name stays
``codex`` throughout: the MCP surface, the ten frozen Agent tools and the writer lease are the real
ones.

Why the fake tunnel-client rather than the real binary: D9 is testing the *launch chain inside*
tunnel-client, not the Control Plane protocol around it. The stand-in really receives
``--mcp.command`` and decodes it with the inverse of the production encoder, so the Windows-specific
quoting contract is covered rather than skipped -- a harness that shelled out would pass even if the
encoder were wrong.

Two invariants the harness itself enforces:

**The environment is planted, not inherited.** The parent deliberately seeds the Tunnel, Control
Plane and proxy namespaces. If they were absent, every scrub assertion below would pass vacuously,
    so
the harness asserts they were present in the tunnel-client before the chain ran.

**Cleanup is unconditional.** Every teardown path terminates the process tree and removes the Job
Object, including on failure. A leaked Bridge from a failing test would hold a writer lease and make
the next test's ``WORKDIR_BUSY`` inexplicable.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_TUNNEL_SOURCE = REPO_ROOT / "tests" / "e2e" / "fake_tunnel_client.py"
BRIDGE_BOOTSTRAP = Path(__file__).resolve().parent / "d9_bridge_bootstrap.py"
BRIDGE_SRC = REPO_ROOT / "agent_bridge" / "src"

#: The interpreter that runs the product CLI (`serverfs_mcp.cli`). Same override
#: discipline as BRIDGE_PYTHON: wheel-only acceptance points this at the ServerFS
#: clean venv instead of a development `.venv` (SERVERFS_TEST_ROOT_PYTHON).
ROOT_PYTHON = Path(
    os.environ.get(
        "SERVERFS_TEST_ROOT_PYTHON",
        str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"),
    )
)
#: The Bridge interpreter the harness launches. Defaults to the development
#: venv layout; ``SERVERFS_BRIDGE_PYTHON`` overrides it -- deliberately the same
#: name the product supervisor honours, because split-environment acceptance
#: (Phase H) points it at the Agent Bridge wheel's own clean venv.
BRIDGE_PYTHON = Path(
    os.environ.get(
        "SERVERFS_BRIDGE_PYTHON",
        str(REPO_ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"),
    )
)

#: Markers planted in the parent so every scrub assertion has something real to catch. The
#: control-plane API key is intentionally excluded because the Windows launcher consumes it from
#: ``--api-key-file`` and now rejects a simultaneous ``CONTROL_PLANE_API_KEY`` source.
CONTROL_PLANE_KEY_VALUE = "d9-not-a-real-key"
TUNNEL_MARKERS: dict[str, str] = {
    "CONTROL_PLANE_TUNNEL_ID": "cp-marker-0002",
    "TUNNEL_CLIENT_PROFILE": "tunnel-marker-0003",
    "SERVERFS_PROXY_PASSWORD": "proxy-marker-0004",
    "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
    "SERVERFS_AGENT_NO_PROXY": "corp.example",
}

#: Lower- and upper-case generic proxy variables, which a partial scrub would leave behind.
GENERIC_PROXY_MARKERS: dict[str, str] = {
    "HTTP_PROXY": "http://generic-marker:8080",
    "HTTPS_PROXY": "http://generic-marker:8443",
    "ALL_PROXY": "http://generic-marker:1080",
    "NO_PROXY": "generic-marker.internal",
    "http_proxy": "http://lower-marker:8080",
    "https_proxy": "http://lower-marker:8443",
}

#: The dedicated Agent endpoint the operator configured, which must reach only the provider child.
AGENT_ENDPOINT_PORT = 18443


@dataclass(frozen=True)
class LifecycleResult:
    """What one full launch chain left behind, for assertions to read."""

    returncode: int | None
    workdir: Path
    data_home: Path
    bridge_config: Path
    lock_dir: Path
    record_dir: Path
    tunnel_record: dict


class Lifecycle:
    """One complete launch chain, driven through ``serverfs tunnel``'s real entry point."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        agent_enabled: bool,
        proxy_enabled: bool = False,
        use_proxy: bool = False,
        read_only: bool = False,
        config_override: str | None = None,
        api_key_outside_workdirs: bool = False,
        bridge_mode: str = "write",
        stderr_is_pipe: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.agent_enabled = agent_enabled
        self.proxy_enabled = proxy_enabled
        self.use_proxy = use_proxy
        self.read_only = read_only
        # Which deterministic behaviour the test-only provider adapter performs. A
        # parameter rather than an environment variable read from outside, because `child_env`
        # builds the Bridge child's environment from scratch: a value set in `os.environ`
        # afterwards is overwritten there, and the adapter then runs its default mode while the
        # caller believes it asked for another. That is not hypothetical -- it is how a run meant
        # to produce an approval quietly produced a plain workspace write instead.
        self.bridge_mode = bridge_mode
        #: Keep the undrained-pipe behaviour when a test is specifically measuring it.
        self.stderr_is_pipe = stderr_is_pipe
        self.stderr_path: Path = tmp_path / "chain-stderr.log"
        self._stderr_handle = None
        # An explicit configuration body, for the launcher-refusal cases that need a chain which is
        # valid enough to be launched but is refused at a chosen earlier step.
        self.config_override = config_override
        # The launcher refuses an API key inside a configured workdir (§7.4). The refusal cases are
        # about a *different* refusal, so they place the key outside every workdir; otherwise they
        # would pass on the key rule and prove nothing about the condition under test.
        self.api_key_outside_workdirs = api_key_outside_workdirs

        self.workdir = tmp_path / "repo"
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "seed.txt").write_text("seed\n", encoding="utf-8")
        # A data home on the same volume as the workdir keeps the lease file creation honest; the
        # private-state contract is verified separately in the Bridge suite.
        self.data_home = tmp_path / "data-home"
        self.record_dir = tmp_path / "records"
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.tunnel_record_path = tmp_path / "tunnel-record.json"
        self.process: subprocess.Popen[bytes] | None = None
        self.bridge_json = self.data_home / "agent-bridge" / "bridge.json"
        self.lock_dir = self.data_home / "agent-bridge" / "locks"
        # The stand-in client is a real python.exe running a script named "run" from this
        # directory, so the launcher child needs it as its working directory.
        self.tunnel_bindir = tmp_path / "tunnel-bin"
        self.tunnel_bindir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FAKE_TUNNEL_SOURCE, self.tunnel_bindir / "run")
        self.bridge_boot = sitecustomize_dir(tmp_path)

    # -- configuration ---------------------------------------------------------------

    def config_path(self) -> Path:
        path = self.tmp_path / "serverfs.toml"
        if not path.exists():
            path.write_text(self.config_override or self._config_text(), encoding="utf-8")
        return path

    def _config_text(self) -> str:
        escaped = str(self.workdir).replace("\\", "\\\\")
        lines = ["[server]", 'log_level = "INFO"', ""]
        if self.agent_enabled:
            lines += ["[agent]", "enabled = true", "", "[agent.codex]", "enabled = true"]
            lines += [f"use_proxy = {str(self.use_proxy).lower()}"]
            if self.proxy_enabled:
                lines += ["", "[agent.proxy]", "enabled = true", 'source = "env"']
            lines += [""]
        lines += [
            "[[workdirs]]",
            'alias = "repo"',
            f'path = "{escaped}"',
            f"read_only = {str(self.read_only).lower()}",
        ]
        if self.agent_enabled:
            lines += ['agent_mode = "workspace-write"', 'agent_runtimes = ["codex"]']
        lines.append("")
        return "\n".join(lines)

    # -- environment -----------------------------------------------------------------

    def _stderr_sink(self):
        """Open (once) the file the chain's stderr goes to, and return the handle for `Popen`."""
        if self._stderr_handle is None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self.stderr_path.open("wb")
        return self._stderr_handle

    def stderr_text(self, *, wait: float = 0.0) -> str:
        """The chain's stderr, from the sink when there is one.

        A caller that wants the output of a process which has just been launched for its refusal
        message passes `wait`: the launcher writes and exits, and reading the file before the write
        lands would return an empty string and look like a silent process. Nothing waits by default,
        so a live chain can still be inspected without blocking.
        """
        if wait > 0 and self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                pass
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

    def child_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The environment the launcher starts with, with every namespace deliberately polluted."""
        env = dict(os.environ)
        for name in list(env):
            # A proxy variable inherited from the real shell would make a scrub assertion vacuous.
            if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                env.pop(name, None)
        env.update(TUNNEL_MARKERS)
        env.update(GENERIC_PROXY_MARKERS)
        env["SERVERFS_DATA_HOME"] = str(self.data_home)
        env["SERVERFS_TEST_RECORD_DIR"] = str(self.record_dir)
        env["SERVERFS_TEST_TUNNEL_RECORD"] = str(self.tunnel_record_path)
        env["SERVERFS_TEST_BRIDGE_MODE"] = self.bridge_mode
        env["BRIDGE_PYTHON"] = str(BRIDGE_PYTHON)
        # The Bridge is a separate distribution in a separate virtualenv, exactly as a deployment
        # would be, so the supervisor needs the interpreter override the doctor also resolves.
        env["SERVERFS_BRIDGE_PYTHON"] = str(BRIDGE_PYTHON)
        # The Bridge child loads the test-only provider adapter as a sitecustomize. This is the only
        # test-specific element in the chain, and it lives entirely inside the Bridge process.
        env["PYTHONPATH"] = str(self.bridge_boot)
        # The Bridge distribution is an editable install resolved through a .pth file, and a
        # A sitecustomize runs before those are processed, so the source directory is named here.
        env["SERVERFS_TEST_BRIDGE_SRC"] = str(BRIDGE_SRC)
        if self.proxy_enabled:
            env["SERVERFS_AGENT_PROXY_URL"] = f"http://127.0.0.1:{AGENT_ENDPOINT_PORT}"
            env["SERVERFS_AGENT_NO_PROXY"] = "corp.example"
        if extra:
            env.update(extra)
        return env

    # -- launching -------------------------------------------------------------------

    def launch(self, *, extra_env: dict[str, str] | None = None) -> LifecycleResult:
        """Start the chain through the real ``serverfs tunnel`` CLI entry point.

        The product entry is ``python -m serverfs_mcp.cli tunnel``, and this launches exactly that.
        An earlier version of this harness imported ``run_native_tunnel`` and called it directly,
        which skipped argparse, ``cmd_tunnel`` and the CLI's error normalization. That is one layer
        short of what an operator runs, and it made a failure the CLI reports as a redacted message
        arrive in the test as a traceback, which looked like a product defect and was only the
        harness's own shortcut.

        Out-of-process because the launcher ends in ``subprocess.run`` of a client that inherits
        stdio: in-process it would take over the test runner's own stdin and stdout.
        """
        api_key = self.tmp_path / "api-key.txt"
        if self.api_key_outside_workdirs:
            # A sibling of the workdir rather than a file inside it, so the launcher's key-location
            # rule does not fire before the condition under test.
            outside = self.tmp_path / "key-material"
            outside.mkdir(parents=True, exist_ok=True)
            api_key = outside / "api-key.txt"
        api_key.write_text(f"{CONTROL_PLANE_KEY_VALUE}\n", encoding="utf-8")
        env_file = self.tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")

        self.process = subprocess.Popen(
            [
                str(ROOT_PYTHON),
                "-m",
                "serverfs_mcp.cli",
                "tunnel",
                "--config",
                str(self.config_path()),
                "--env-file",
                str(env_file),
                # The stand-in client is python.exe running the harness script named "run" from the
                # binding directory, which is why that directory is this process's cwd.
                "--tunnel-client",
                str(ROOT_PYTHON),
                "--tunnel-id",
                "tunnel_" + "a" * 32,
                "--api-key-file",
                str(api_key),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # A file, not a pipe, unless a test asks for the old behaviour. The whole native tree
            # inherits this handle and the product logger writes each record with a synchronous
            # `sys.stderr.write` + `flush` on the serving event loop, so an undrained pipe is a
            # finite buffer that eventually blocks the loop inside a handler. That presents as a
            # ServerFS tool that stopped answering, which is the wrong conclusion to draw. The pipe
            # behaviour stays available so the difference can be measured, not argued about.
            stderr=(subprocess.PIPE if self.stderr_is_pipe else self._stderr_sink()),
            env=self.child_env(extra_env),
            cwd=str(self.tunnel_bindir),
        )
        return self.result()

    def result(self) -> LifecycleResult:
        record: dict = {}
        if self.tunnel_record_path.exists():
            try:
                record = json.loads(self.tunnel_record_path.read_text(encoding="utf-8"))
            except ValueError:
                record = {}
        return LifecycleResult(
            returncode=self.process.returncode if self.process else None,
            workdir=self.workdir,
            data_home=self.data_home,
            bridge_config=self.bridge_json,
            lock_dir=self.lock_dir,
            record_dir=self.record_dir,
            tunnel_record=record,
        )

    # -- teardown --------------------------------------------------------------------

    def stop(self, *, timeout: float = 30.0) -> int | None:
        """Close the harness's stdin and wait, which is how the real client shuts its child down."""
        if self.process is None:
            return None
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
        return self.process.returncode

    def kill(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass

    def bridge_pids(self) -> list[int]:
        """Every live process whose command line mentions this data home's Bridge config.

        Matched on the config path rather than on a process name, because the Bridge, the stdio
            child
        and the supervisor are all ``python.exe`` here.
        """
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


#: Resolved by absolute path: a bare "powershell" on PATH is not reliably executable from a
#: ``CreateProcess`` launched by a test runner, and the failure mode is a confusing WinError 193.
POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)


def process_command_lines() -> list[tuple[int, str]]:
    """``(pid, command line)`` for every live process, via PowerShell CIM.

    Matched on the command line rather than the image name because the Bridge, the stdio child and
        the
    supervisor are all ``python.exe`` on this host. ``wmic`` is not used: it is deprecated and
        absent on
    current Windows, which would make the containment assertions silently vacuous.
    """
    if not POWERSHELL.exists():
        return []
    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | "
            'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }',
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=90,
    )
    found: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        pid_text, _, command_line = line.partition("\t")
        if pid_text.strip().isdigit():
            found.append((int(pid_text.strip()), command_line))
    return found


def _alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259


def wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    """Poll until ``predicate`` holds. Returns whether it did; never raises on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fresh_name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def require_windows() -> None:
    if sys.platform != "win32":  # pragma: no cover - the whole module is Windows-only
        raise RuntimeError("the D9 native lifecycle acceptance is Windows-only")


def interpreters_present() -> bool:
    return ROOT_PYTHON.exists() and BRIDGE_PYTHON.exists()


def sitecustomize_dir(tmp_path: Path) -> Path:
    """Make the test-only Bridge bootstrap importable as ``sitecustomize`` for the Bridge child.

    ``sitecustomize`` is imported by name during interpreter start, so the file must literally be
    called ``sitecustomize.py`` and be on ``sys.path``. A copy is used rather than a rename so the
        file
    keeps its descriptive name in the repository.
    """
    boot = tmp_path / "bridge-boot"
    boot.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BRIDGE_BOOTSTRAP, boot / "sitecustomize.py")
    return boot


def tunnel_client_executable(tmp_path: Path) -> Path:
    """A ``CreateProcess``-able stand-in for tunnel-client, and why it is built this way.

    ``native_tunnel`` execs the client path directly, so the stand-in must be a real executable
        image.
    A ``.cmd`` shim is not one -- ``CreateProcess`` refuses it with WinError 193 -- and neither is a
    ``.py`` file. So the harness points the launcher at ``python.exe`` and drops the harness beside
        it
    under the name ``run``, which is the subcommand ``native_tunnel`` passes as ``argv[1]``. Python
    resolves ``run`` as a script in the current directory, which gives a genuine executable that
    receives the real argv and decodes ``--mcp.command`` for real.
    """
    bindir = tmp_path / "tunnel-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FAKE_TUNNEL_SOURCE, bindir / "run")
    return ROOT_PYTHON
