"""I5 exact Scheduler failed-start evidence, without host mutation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-scheduler-postfail-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_scheduler_evidence", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "00000000"), (-2147024891, "80070005"), (2147942405, "80070005")],
)
def test_i5_scheduler_result_preserves_unsigned_hresult(value: int, expected: str) -> None:
    mod = _module()
    import json

    code, ran = mod.parse_info(json.dumps({"result": value, "ever_run": True}).encode("utf-8"))
    assert f"{code:08X}" == expected
    assert ran is True


@pytest.mark.parametrize("body", [b"{}", b"null", b'{"result": true, "ever_run": true}'])
def test_i5_scheduler_last_result_requires_known_shape(body: bytes) -> None:
    module = _module()
    with pytest.raises(module.I5SchedulerEvidenceError):
        module.parse_info(body)


def test_i5_scheduler_evidence_has_no_commands_that_mutate_services() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    for required in (
        "Get-ScheduledTaskInfo",
        "scheduler._task_xml(owner)",
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "_current_identity(owner) is None",
        "I5_SCHEDULER_POSTFAIL_READONLY=PASS",
    ):
        assert required in source
    for forbidden in (
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "storage.stage(",
        "print(result.stdout)",
        ".write_text(",
        ".write_bytes(",
        "git push",
        "secret_hex",
    ):
        assert forbidden not in source
