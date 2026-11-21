"""Read-only native Claude Provider readiness probe for an installed I4 worker.

This is NOT a task, model call, CLI --version invocation, or credential test.
The probe imports the actual installed Claude Agent SDK and ClaudeAdapter,
then resolves the current-user Claude executable without executing it.

Run only from i4-a's installed agent-bridge Python with -I -B. On missing
SDK/CLI, fail closed without touching Scheduler, process tree, or provider.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class I5ProviderPreflightError(RuntimeError):
    """Provider availability is unverified; never leak an executable path."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5ProviderPreflightError(reason)


def main() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated installed interpreter with -I -B required",
    )
    local = os.environ.get("LOCALAPPDATA", "")
    require(bool(local), "Windows LocalAppData is unavailable")
    expected = (
        Path(local)
        / "ServerFS"
        / "v25"
        / "instances"
        / "i4-a"
        / "envs"
        / "agent-bridge"
        / "Scripts"
        / "python.exe"
    )
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "installed I4 Agent Bridge worker interpreter required",
    )
    print("I5_INSTALLED_WORKER_INTERPRETER=VERIFIED")

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from serverfs_agent_bridge.adapters.claude import ClaudeAdapter, _resolve_cli
    from serverfs_agent_bridge.config import ClaudeSettings

    require(
        ClaudeSDKClient.__module__.startswith("claude_agent_sdk")
        and ClaudeAgentOptions.__module__.startswith("claude_agent_sdk"),
        "official Claude SDK objects unavailable",
    )
    require(
        ClaudeAdapter(ClaudeSettings()).name == "claude",
        "installed Claude Adapter import or identity differs",
    )
    print("I5_REAL_CLAUDE_SDK_ADAPTER_IMPORT=PASS")

    cli = _resolve_cli(ClaudeSettings().claude_bin)
    require(cli is not None, "Claude CLI not resolved in installed worker environment")
    path = Path(cli)
    require(
        path.is_file() and os.access(path, os.X_OK),
        "resolved Claude CLI is not an executable file",
    )
    print("I5_NATIVE_CLAUDE_CLI_RESOLVED=PASS")
    print("I5_REAL_PROVIDER_TASK=NOT_RUN")
    print("I5_READONLY_PROVIDER_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - reason/path/secret not displayed
        print("I5_PROVIDER_PREFLIGHT=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
