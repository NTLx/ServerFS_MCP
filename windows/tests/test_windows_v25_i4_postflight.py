"""I4 native read-only postflight code and safety contracts.

No installed Scheduler access, service changes, credentials or network calls.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i4-postflight-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i4_postflight_contract", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_postflight_only_checks_existing_i4_pair_resources() -> None:
    module = _module()
    assert module.IDS == ("i4-a", "i4-b")
    assert module.PORTS == (18871, 18872)
    assert set(module.IDS).isdisjoint({"default", "i1-smoke", "i2-smoke", "i3-shared"})


def test_postflight_refuses_false_checks() -> None:
    module = _module()
    module.require(True, "ok")
    with pytest.raises(module.I4PostflightError):
        module.require(False, "I4 fixture is not offline")


def test_postflight_source_is_readonly_and_fail_closed() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)",
        "status = Scheduler().status(manifest)",
        "_current_identity(manifest) is None",
        "_windows_ipv4_tcp_listeners()",
        "storage.load(component, instance)",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "IsUserAnAdmin()",
        "I4_POSTFLIGHT_FOUR_TASKS_DISABLED_OFFLINE=PASS",
        "I4_POSTFLIGHT_PORTS_18871_18872_UNBOUND=PASS",
        "I4_POSTFLIGHT_READONLY=PASS",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.prepare(",
        "storage.stage(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Stop-Process",
        "Remove-Item",
        "secret_hex",
        "git push",
    ):
        assert forbidden not in source
