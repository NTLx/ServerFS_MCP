"""I5 disposable *real Claude SDK* canary worker.

Invoked only by v0250-i5-claude-canary-host.py inside a fresh kill-on-close
Windows Job. One short provider inference is performed with no tools or MCP
servers, in a temporary cwd. No ServerFS service or Task Scheduler state is
read or changed. May create the provider's ordinary user session history.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import sys
from pathlib import Path

SENTINEL = "I5_CANARY_OK"
DISALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TaskOutput",
    "TaskStop",
    "Skill",
]


class I5CanaryError(RuntimeError):
    """Do not expose native model or credential errors in parent output."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5CanaryError(message)


async def _run(cwd: Path, cli: str) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
    from claude_agent_sdk.types import PermissionResultDeny

    denied_calls = 0

    async def deny_tool(_tool: str, _args: dict, _context):
        nonlocal denied_calls
        denied_calls += 1
        return PermissionResultDeny(message="No tools permitted by I5 canary", interrupt=False)

    options = ClaudeAgentOptions(
        cwd=str(cwd),
        cli_path=cli,
        setting_sources=[],
        allowed_tools=[],
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="default",
        can_use_tool=deny_tool,
        max_turns=1,
        system_prompt=f"Return exactly the text {SENTINEL}. Do not call tools.",
    )
    client = ClaudeSDKClient(options)
    try:
        await client.connect()
        await client.query(f"Respond with exactly {SENTINEL}. No other text. No tool calls.")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                response = getattr(message, "result", None)
                require(
                    getattr(message, "is_error", True) is False
                    and isinstance(response, str)
                    and response.strip() == SENTINEL
                    and denied_calls == 0,
                    "real Claude canary response or no-tools policy not verified",
                )
                print("I5_REAL_CLAUDE_MODEL_RESPONSE=PASS")
                print("I5_CLAUDE_TOOL_CALLS=ZERO")
                return
        raise I5CanaryError("Claude SDK stream ended without a result")
    finally:
        await client.disconnect()


def main() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows -I -B worker required",
    )
    require(len(sys.argv) == 2, "I5 canary requires one disposable workdir")
    path = Path(sys.argv[1])
    require(path.is_absolute() and path.is_dir() and not path.is_symlink(), "I5 cwd invalid")
    # The Provider is allowed to use the current user's native authentication,
    # but the test must never inspect/serialize any credential or session.
    local = os.environ.get("LOCALAPPDATA", "")
    require(bool(local), "Windows provider worker home unavailable")
    installed = (
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
        Path(sys.executable).resolve(strict=True) == installed.resolve(strict=True),
        "I5 requires installed real Agent Bridge worker Python",
    )
    cli = shutil.which("claude")
    require(cli is not None and Path(cli).is_file(), "real Claude CLI missing")
    asyncio.run(asyncio.wait_for(_run(path, cli), timeout=95))
    print("I5_CANARY_WORKER=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - never emit native error content
        print("I5_CANARY_WORKER=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
