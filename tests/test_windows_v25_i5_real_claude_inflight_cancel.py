"""Source-only safety contracts for an explicit one-use installed I5 cancel gate.

These tests never start installed Scheduler tasks, the Claude Provider, a
network connection, tunnel, process or agent runtime.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-real-claude-inflight-cancel-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_cancel_safe_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_cancel_is_exact_explicit_three_service_fixture() -> None:
    mod = _module()
    assert mod.BRIDGE == "i5-shared"
    assert mod.CORES == {"i5-core-a": 18873, "i5-core-b": 18874}
    assert mod.TERMINAL == {"succeeded", "failed", "cancelled", "interrupted"}
    with pytest.raises(mod.I5CancelGateError):
        mod.require(False, "safe-error")


def test_i5_cancel_requires_one_model_task_via_core_http_mcp() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        'sys.argv[1:] == ["--run-once"]',
        "Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True)",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "streamable_http_client(",
        '"submit_agent_task"',
        '"runtime": "claude"',
        '"workdir": "Fixture"',
        '"profile": "workspace-write"',
        "trust_env=False",
        '"I5_CANCEL_SUBMITTED_VIA_CORE_A_HTTP=PASS"',
    ):
        assert fragment in source
    assert source.count('"submit_agent_task"') == 1
    assert 'client_a.call("task.submit"' not in source


def test_i5_cancel_requires_proof_of_running_and_signed_foreign_denial() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        'status == "running"',
        '"task.started"',
        '"provider-finished-before-running-probe"',
        '"not-running-before-cancel"',
        '"CORE_TASK_NOT_FOUND"',
        'for method in ("task.get", "task.events", "task.result.read", "task.cancel")',
        '"I5_CANCEL_FOREIGN_CORE_FOUR_METHODS=DENIED"',
        '"I5_CANCEL_BRIDGE_TASK_RUNNING_AND_STARTED=PASS"',
        "real.assert_no_unsafe_event(events)",
    ):
        assert fragment in source


def test_i5_cancel_proves_owner_terminal_and_disallows_false_success() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        '"task.cancel", {"workdir": "Fixture", "task_id": task_id}',
        'outcome.get("status") == "cancelled"',
        'task.get("status") == "cancelled"',
        '"task.cancelled"',
        '"task.completed" not in kinds',
        'print("I5_CANCEL_PRIVATE_EVENT_AUDIT=PASS")',
        "prove_native_session_after_stop(verified_task_id)",
        '"v0250-i5-cancel-state-proof-worker.py"',
        'print("I5_CANCEL_RUNNING_WARMUP=PASS")',
        "warmup_deadline = time.monotonic() + 8.0",
        'input=task_id.encode("ascii")',
        '"I5_CANCEL_PRIVATE_CLAUDE_INIT_AND_DURABLE_OWNER=PASS"',
        'print("I5_REAL_PROVIDER_INFLIGHT_OWNER_CANCEL=PASS")',
        "if task_id is not None and not cancelled:",
    ):
        assert fragment in source


def test_i5_cancel_preserves_protected_services_and_always_cleans_up() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "smoke.protected_snapshot(gate)",
        "smoke.candidates_disabled(gate)",
        '"fixture-workdir-policy-drift"',
        'started.append(("bridge", BRIDGE))',
        'started.append(("core", core))',
        "for component, instance in reversed(started):",
        'smoke.cli(component, instance, "stop")',
        'print("I5_CANCEL_THREE_FIXTURES_STOP="',
        "after.creation_time == generation.creation_time",
    ):
        assert fragment in source
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.prepare(",
        "storage.stage(",
        "shutil.rmtree(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "git push",
        "git commit",
        "ANTHROPIC_API_KEY",
        "print(task_id)",
        "print(submitted)",
        "print(keys)",
        "print(events)",
        "print(token)",
    ):
        assert forbidden not in source
