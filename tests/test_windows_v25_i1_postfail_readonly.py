"""Pure tests for I1 postfailed smoke read-only owner inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deployment" / "windows" / "v0250-i1-postfail-readonly.py"


def module():
    spec = importlib.util.spec_from_file_location("_i1_postfail", _SCRIPT)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def _inventory():
    script = module()
    tasks = [
        {
            "name": name,
            "count": 1,
            "state": "Running" if running else "Disabled",
            "enabled": enabled,
        }
        for name, running, enabled in script.PRODUCTION
    ]
    tasks.append(
        {
            "name": script.TASK,
            "count": 1,
            "state": "Disabled",
            "enabled": False,
        }
    )
    return {"tasks": tasks, "listeners": []}


def test_fixed_read_only_identity_and_scope():
    script = module()
    source = _SCRIPT.read_text(encoding="utf-8")
    assert script.INSTANCE == "i1-smoke"
    assert script.PORT == 18868
    assert script.TASK == "ServerFS v25 i1-smoke Core"
    assert "Get-ScheduledTask" in source
    assert "Get-NetTCPConnection" in source
    assert 'storage.load("core", instance)' in source
    assert "_current_identity(manifest)" in source
    for forbidden in (
        "Stop-ScheduledTask",
        "Start-ScheduledTask",
        "Enable-ScheduledTask",
        "Disable-ScheduledTask",
        "Remove-Item",
        "SERVERFS_HTTP_API_KEY",
        "http.env",
        "auth_file",
    ):
        assert forbidden not in source


def test_parse_known_six_task_inventory():
    script = module()
    tasks, listeners = script.parse_inventory(_inventory())
    assert listeners == []
    assert tasks[script.TASK]["state"] == "Disabled"


def test_summarize_healthy_snapshot(capsys):
    script = module()
    tasks, ports = script.parse_inventory(_inventory())
    script.summarize_inventory(tasks, ports)
    printed = capsys.readouterr().out
    assert "I1_POSTFAIL_PRODUCTION_TASK_BASELINE=PASS" in printed
    assert "I1_POSTFAIL_FIXTURE_TASK_STATE=Disabled" in printed
    assert "I1_POSTFAIL_FIXTURE_TASK_ENABLED=False" in printed
    assert "I1_POSTFAIL_LOOPBACK_18868_LISTENERS=0" in printed


def test_summarize_drift_flags_without_mutation(capsys):
    script = module()
    source = _inventory()
    source["tasks"][0]["state"] = "Ready"
    source["tasks"][-1]["state"] = "Running"
    source["tasks"][-1]["enabled"] = True
    tasks, ports = script.parse_inventory(source)
    script.summarize_inventory(tasks, ports)
    printed = capsys.readouterr().out
    assert "I1_POSTFAIL_PRODUCTION_TASK_BASELINE=DIFFERS" in printed
    assert "I1_POSTFAIL_FIXTURE_TASK_STATE=Running" in printed
    assert "I1_POSTFAIL_FIXTURE_TASK_ENABLED=True" in printed


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        {"tasks": [], "listeners": []},
        {"tasks": [None] * 6, "listeners": []},
        {"tasks": [{"name": "duplicate"}] * 6, "listeners": []},
    ],
)
def test_parse_rejects_ambiguous_inventory(source):
    with pytest.raises(ValueError):
        module().parse_inventory(source)
