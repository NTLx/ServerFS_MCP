"""I5 post-failure Claude SDK options diagnostic with NO model or CLI process.

Runs under the same installed i4-a Agent Bridge worker interpreter as the
failed canary. Validates options and SDK client construction only, and reports
whether certain nonsecret nested-CLI environment *names* are present. It does
not open authentication files, inspect variable values or connect/query.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import sys
from pathlib import Path

PHASE = "entry"
NESTED_NAMES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_SESSION_ID")


class I5OptionsDiagnosticError(RuntimeError):
    """Fail closed without showing native configuration or exception text."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5OptionsDiagnosticError(message)


def main() -> None:
    global PHASE
    PHASE = "interpreter"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 diagnostic requires native non-elevated Windows -I -B",
    )
    home = os.environ.get("LOCALAPPDATA", "")
    require(bool(home), "I5 worker user home missing")
    installed = (
        Path(home)
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
        Path(sys.executable).resolve(strict=True) == installed.resolve(strict=True),
        "I5 diagnostic requires the installed Agent Bridge SDK interpreter",
    )

    PHASE = "sdk-options"
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import PermissionResultDeny

    require(dataclasses.is_dataclass(ClaudeAgentOptions), "SDK options are not a dataclass")
    names = {item.name for item in dataclasses.fields(ClaudeAgentOptions)}
    require(
        {
            "cwd",
            "cli_path",
            "setting_sources",
            "allowed_tools",
            "disallowed_tools",
            "permission_mode",
            "can_use_tool",
            "max_turns",
            "system_prompt",
        }
        <= names,
        "installed SDK lacks exact no-tools canary options",
    )
    print("I5_CLAUDE_OPTIONS_CONTRACT=PASS")

    async def deny(_tool, _arguments, _context):
        return PermissionResultDeny(message="Canary tools disabled")

    options = ClaudeAgentOptions(
        cwd=str(Path(home).resolve()),
        cli_path="claude",
        setting_sources=[],
        allowed_tools=[],
        disallowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "WebFetch",
            "WebSearch",
            "Task",
        ],
        permission_mode="default",
        can_use_tool=deny,
        max_turns=1,
        system_prompt="Return one short value. Never use tools.",
    )
    PHASE = "sdk-constructor"
    client = ClaudeSDKClient(options)
    require(isinstance(client, ClaudeSDKClient), "SDK constructor returned invalid client")
    print("I5_CLAUDE_SDK_CONSTRUCTOR=PASS")

    PHASE = "ambient-name-presence"
    present = any(name in os.environ for name in NESTED_NAMES)
    print("I5_NESTED_CLAUDE_CONTEXT=" + ("PRESENT" if present else "ABSENT"))
    print("I5_CLAUDE_MODEL_CALL=NOT_RUN")
    print("I5_OPTIONS_DIAGNOSTIC=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only fixed phase and exception type
        print(
            "I5_OPTIONS_DIAGNOSTIC=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
