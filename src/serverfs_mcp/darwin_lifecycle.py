"""The macOS Agent Bridge lifecycle under a per-user LaunchAgent (v0.13 Phase E).

Topology (dev_plan_v0.13.md §12 E5): launchd owns a persistent Agent
Bridge; the OpenAI tunnel supervises a stdio ServerFS MCP child that
connects to the Bridge's AF_UNIX endpoint. launchd is a service manager,
not a containment kernel: no Job Object equivalence is claimed (§12 E4),
so a shutdown that cannot prove the provider descendants stopped keeps
the recovery guard and workspace-write fails closed — that behavior
belongs to the Bridge's existing recovery logic and is unchanged here.

Rules frozen by the plan:

- current-user LaunchAgent only: no root, no LaunchDaemon;
- modern launchctl verbs only (bootstrap / print / kickstart / bootout);
- the plist carries paths, never secrets — provider credentials and
  proxy material stay in the Bridge's private runtime/config material;
- no shell-profile dependency: every path is resolved explicitly;
- no machine-specific path is committed to the repository — the plist
  is generated at install time from the live environment.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

AGENT_LABEL = "com.ntlx.serverfs.agent-bridge"

#: Grace period launchd allows for the Bridge's graceful SIGTERM path
#: (signal handlers + socket close) before SIGKILL.
EXIT_TIMEOUT_SECONDS = 30


class LaunchAgentError(Exception):
    """Operator-facing lifecycle failure; messages never contain secrets."""


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def launch_agent_plist_path() -> Path:
    return launch_agents_dir() / f"{AGENT_LABEL}.plist"


def default_bridge_executable() -> Path:
    """The console script next to this interpreter (``uv sync`` layout)."""
    candidate = Path(sys.executable).parent / "serverfs-agent-bridge"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise LaunchAgentError(
        "serverfs-agent-bridge executable not found beside the active interpreter; "
        "pass --bridge-python or run 'uv sync --project agent_bridge'"
    )


@dataclass(frozen=True)
class LaunchAgentPlan:
    """Everything the generated plist carries — paths only, no secrets."""

    bridge_executable: Path
    bridge_config_path: Path
    log_dir: Path
    label: str = AGENT_LABEL

    def render(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": [
                    str(self.bridge_executable),
                    "--config",
                    str(self.bridge_config_path),
                ],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
                "StandardOutPath": str(self.log_dir / "bridge.stdout.log"),
                "StandardErrorPath": str(self.log_dir / "bridge.stderr.log"),
            },
            sort_keys=True,
        )


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchAgentError(
            f"launchctl {' '.join(args[:2])} failed ({type(exc).__name__})"
        ) from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[0] if detail else f"exit {completed.returncode}"
        raise LaunchAgentError(f"launchctl {' '.join(args[:2])} refused: {reason}")
    return completed


def install(plan: LaunchAgentPlan, *, force: bool = False) -> Path:
    """Write the generated plist and bootstrap the agent into this user's gui domain."""
    if not plan.bridge_executable.is_file():
        raise LaunchAgentError("bridge executable not found")
    if not plan.bridge_config_path.is_file():
        raise LaunchAgentError("bridge configuration file not found")
    plist_path = launch_agent_plist_path()
    if plist_path.exists() and not force:
        raise LaunchAgentError(
            f"{plist_path} already exists; pass force to reinstall (this re-bootstraps the agent)"
        )
    # A LaunchAgent with KeepAlive boots on login and restarts on crash; the
    # label is fixed, so an existing running agent must be booted out first.
    _run_launchctl("bootout", f"{_gui_domain()}/{AGENT_LABEL}", check=False)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plan.log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plan.render())
    os.chmod(plist_path, 0o644)
    _run_launchctl("bootstrap", _gui_domain(), str(plist_path))
    return plist_path


def start() -> None:
    _run_launchctl("kickstart", f"{_gui_domain()}/{AGENT_LABEL}")


def restart() -> None:
    _run_launchctl("kickstart", "-k", f"{_gui_domain()}/{AGENT_LABEL}")


def stop() -> None:
    _run_launchctl("bootout", f"{_gui_domain()}/{AGENT_LABEL}")


def status() -> dict[str, str]:
    """Read-only state from ``launchctl print``; never starts or stops anything."""
    completed = _run_launchctl("print", f"{_gui_domain()}/{AGENT_LABEL}", check=False)
    if completed.returncode != 0:
        return {"state": "not-bootstrapped"}
    state = "unknown"
    pid = ""
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped.removeprefix("state = ")
        elif stripped.startswith("pid = "):
            pid = stripped.removeprefix("pid = ")
    return {"state": state, "pid": pid}


def uninstall() -> None:
    """Boot the agent out and remove the generated plist."""
    _run_launchctl("bootout", f"{_gui_domain()}/{AGENT_LABEL}", check=False)
    plist_path = launch_agent_plist_path()
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
