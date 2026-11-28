"""Windows I3 multi-Core/shared-Bridge read-only preflight contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i3-shared-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("serverfs_i3_preflight", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(module):
    return [
        {"name": name, "enabled": enabled, "state": state or "Disabled"}
        for name, (enabled, state) in module.PRODUCTION.items()
    ]


def test_i3_three_different_instance_identities_and_ports() -> None:
    module = _module()
    assert module.BRIDGE_ID == "i3-shared"
    assert module.CORE_PORTS == {"i3-core-a": 18869, "i3-core-b": 18870}
    assert len(module.TARGETS) == 3
    assert "ServerFS v25 i3-shared Agent Bridge" in module.TARGETS
    assert "ServerFS v25 i3-core-a Core" in module.TARGETS
    assert "ServerFS v25 i3-core-b Core" in module.TARGETS
    assert module.verify_task_inventory(_rows(module)) is None
    assert module.verify_ports_free([SimpleNamespace(port=18868)]) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].update(enabled=False),
        lambda rows: rows[1].update(state="Disabled"),
        lambda rows: rows[2].update(state="Ready"),
        lambda rows: rows[3].update(state="Running"),
        lambda rows: rows[3].update(state="Ready"),
        lambda rows: rows[4].update(state="Ready"),
        lambda rows: rows[4].update(state="Queued"),
        lambda rows: rows[0].update(enabled="true"),
        lambda rows: rows.append(
            {"name": "ServerFS v25 i3-core-a Core", "enabled": False, "state": "Disabled"}
        ),
        lambda rows: rows.append(
            {
                "name": "ServerFS v25 i3-shared Agent Bridge",
                "enabled": False,
                "state": "Disabled",
            }
        ),
        lambda rows: rows[0].update(name=[]),
    ],
)
def test_i3_refuses_production_drift_or_existing_i3_task(change) -> None:
    module = _module()
    rows = _rows(module)
    change(rows)
    with pytest.raises(module.I3PreflightError):
        module.verify_task_inventory(rows)


@pytest.mark.parametrize("port", [18869, 18870])
def test_i3_rejects_any_existing_tcp_listener_even_nonloopback(port: int) -> None:
    module = _module()
    with pytest.raises(module.I3PreflightError, match="occupied"):
        module.verify_ports_free([SimpleNamespace(port=port)])


def test_i3_rejects_missing_listener_evidence() -> None:
    module = _module()
    with pytest.raises(module.I3PreflightError):
        module.verify_ports_free(None)
    with pytest.raises(module.I3PreflightError):
        module.verify_ports_free([object()])


def test_i3_preflight_contains_no_mutating_operations() -> None:
    module = _module()
    source = _SCRIPT.read_text("utf-8")
    assert "Get-ScheduledTask" in module.SCRIPT
    assert "New-ScheduledTask" not in source
    assert "Register-ScheduledTask" not in source
    assert "Stop-ScheduledTask" not in source
    assert "Stop-Process" not in source
    assert "storage.prepare" not in source
    assert "core-grants.json" not in source
    assert "http.env" not in source
    assert "Scheduler().install" not in source
    assert "I3_NATIVE_PREFLIGHT=PASS" in source
    assert "-NonInteractive" in source
    assert "subprocess.DEVNULL" in source
