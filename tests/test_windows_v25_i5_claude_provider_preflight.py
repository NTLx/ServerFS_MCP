"""I5 installed-provider SDK/CLI inspection contracts; no real provider calls."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-claude-provider-readonly-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_claude_probe_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_probe_refuses_false_preconditions() -> None:
    module = _module()
    module.require(True, "ok")
    with pytest.raises(module.I5ProviderPreflightError):
        module.require(False, "absent")


def test_i5_probe_is_readonly_and_installed_worker_scoped() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        '"i4-a"',
        '"agent-bridge"',
        '"python.exe"',
        "Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True)",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "IsUserAnAdmin()",
        "from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient",
        "from serverfs_agent_bridge.adapters.claude import ClaudeAdapter, _resolve_cli",
        "_resolve_cli(ClaudeSettings().claude_bin)",
        "I5_INSTALLED_WORKER_INTERPRETER=VERIFIED",
        "I5_REAL_CLAUDE_SDK_ADAPTER_IMPORT=PASS",
        "I5_NATIVE_CLAUDE_CLI_RESOLVED=PASS",
        "I5_REAL_PROVIDER_TASK=NOT_RUN",
        "I5_READONLY_PROVIDER_PREFLIGHT=PASS",
    ):
        assert required in source
    for forbidden in (
        "subprocess.",
        ".probe(",
        ".run_task(",
        ".query(",
        ".connect(",
        "request.",
        'os.getenv("ANTHROPIC',
        'os.environ["ANTHROPIC',
        "Scheduler()",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "os.system(",
        "git push",
        "git commit",
        "secret_hex",
    ):
        assert forbidden not in source
