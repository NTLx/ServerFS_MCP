"""I5 Claude real-provider canary safety contracts; source-only.

These checks must never invoke the canary, the SDK, a task or any network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "deployment" / "windows"
_HOST = _ROOT / "v0250-i5-claude-canary-host.py"
_WORKER = _ROOT / "v0250-i5-claude-canary-worker.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canary_worker_is_one_model_turn_with_no_tools_and_no_settings() -> None:
    worker = _load(_WORKER, "i5_worker_contract")
    assert worker.SENTINEL == "I5_CANARY_OK"
    assert {"Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"} <= set(
        worker.DISALLOWED_TOOLS
    )
    source = _WORKER.read_text("utf-8")
    for fragment in (
        "ClaudeSDKClient(options)",
        "setting_sources=[]",
        "allowed_tools=[]",
        "disallowed_tools=DISALLOWED_TOOLS",
        "max_turns=1",
        "can_use_tool=deny_tool",
        "PermissionResultDeny(",
        "denied_calls == 0",
        "response.strip() == SENTINEL",
        "asyncio.wait_for(_run(path, cli), timeout=95)",
        "I5_CLAUDE_TOOL_CALLS=ZERO",
    ):
        assert fragment in source
    for forbidden in (
        "Scheduler()",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "os.system(",
        "git push",
        "git commit",
        "request.get(",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source


def test_canary_supervisor_requires_explicit_single_use_and_job_containment() -> None:
    host = _load(_HOST, "i5_host_contract")
    with pytest.raises(host.I5CanaryHostError):
        host.require(False, "blocked")
    source = _HOST.read_text("utf-8")
    for fragment in (
        'sys.argv[1:] == ["--run-once"]',
        "with WindowsJob() as job:",
        "subprocess.CREATE_SUSPENDED",
        "job.assign(child)",
        "job.resume(child)",
        "child.communicate(timeout=115)",
        "job.close()",
        "tempfile.TemporaryDirectory(",
        '"I5_REAL_CLAUDE_MODEL_RESPONSE=PASS"',
        '"I5_CLAUDE_TOOL_CALLS=ZERO"',
        '"I5_CANARY_WORKER=PASS"',
        "I5_WINDOWS_JOB_CONTAINMENT=VERIFIED",
        "I5_REAL_PROVIDER_CANARY=PASS",
    ):
        assert fragment in source
    for forbidden in (
        "Scheduler()",
        "storage.prepare(",
        "storage.stage(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "os.system(",
        "git push",
        "git commit",
    ):
        assert forbidden not in source
