"""I5 Bridge failed-start evidence collector: no side effects on the host."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-bridge-postfail-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_bridge_postfailure", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_i5_bridge_runtime_log_parser_is_exact_and_sanitized() -> None:
    mod = _module()
    raw = (
        "secret=do-not-expose\n"
        "windows-v25: bridge failed (BridgeV2OwnerError)\n"
        "windows-v25: core failed (OtherError)\n"
        "windows-v25: bridge failed (LegacyBridgeOwnerError)\n"
    )
    assert mod.failure_types(raw) == ("BridgeV2OwnerError", "LegacyBridgeOwnerError")
    assert mod.failure_types("user-data-only") == ()


def test_i5_failure_classifier_refuses_unexpected_host_state() -> None:
    mod = _module()
    mod.require(True, "ok")
    with pytest.raises(mod.I5PostfailureError):
        mod.require(False, "unknown")


def test_i5_postfailure_script_has_no_mutating_scheduler_operations() -> None:
    source = _SOURCE.read_text("utf-8")
    for marker in (
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "Scheduler().status(manifest)",
        "_current_identity(manifest) is None",
        "load_native_bridge_plan(manifest)",
        "assert_legacy_writable_roots_disjoint(manifest)",
        "storage.check_private(log",
        "I5_BRIDGE_POSTFAIL_LOG_CLASS_COUNT=",
        "I5_BRIDGE_POSTFAIL_READONLY=PASS",
    ):
        assert marker in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.stage(",
        "storage.prepare(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "print(blob)",
        "print(raw)",
        "git push",
        "secret_hex",
        ".write_text(",
        ".write_bytes(",
    ):
        assert forbidden not in source
