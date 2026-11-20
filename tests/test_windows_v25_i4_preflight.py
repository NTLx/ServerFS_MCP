"""I4 independent Core/Bridge read-only preflight contracts (source-only).

Does not run Windows Scheduler, use native credentials, or change host state.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from serverfs_mcp.instance_identity import InstanceIdentity

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i4-independent-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i4_preflight_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(module, *, candidates: bool = False):
    rows = [
        {"name": name, "state": state, "enabled": enabled}
        for name, (enabled, state) in module.PRODUCTION.items()
    ]
    rows.extend(
        {"name": name, "state": "Disabled", "enabled": False} for name in module.EXISTING_FIXTURES
    )
    if candidates:
        rows.extend(
            {"name": name, "state": "Disabled", "enabled": False} for name in module.TARGETS
        )
    return rows


def test_i4_reserved_identity_is_two_owned_core_bridge_pairs() -> None:
    mod = _module()
    assert len(mod.BRIDGES) == len(mod.CORE_PORTS) == 2
    assert len(mod.TARGETS) == 4
    assert len(set(mod.TARGETS)) == 4
    assert len(set(mod.CORE_PORTS.values())) == 2
    assert not set(mod.TARGETS).intersection(mod.PRODUCTION)
    assert not set(mod.TARGETS).intersection(mod.EXISTING_FIXTURES)
    assert set(mod.PAIRS) == set(mod.CORE_PORTS)
    assert set(mod.PAIRS.values()) == set(mod.BRIDGES)
    assert all(core_id == bridge_id for core_id, bridge_id in mod.PAIRS.items())
    for identity in mod.BRIDGES:
        assert InstanceIdentity(identity, "owned", identity).bridge_id == identity
    assert all(id_.startswith("i4-") for id_ in (*mod.BRIDGES, *mod.CORE_PORTS))


def test_i4_preflight_accepts_exact_protected_baseline_and_disabled_postflight() -> None:
    mod = _module()
    mod.verify_task_rows(_rows(mod))
    mod.verify_task_rows(_rows(mod, candidates=True), require_candidates_absent=False)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "active_fixture",
        "production_stopped",
        "occupied",
        "duplicate",
        "extra",
        "wrong_enable_type",
    ],
)
def test_i4_preflight_refuses_changed_or_occupied_protected_tasks(case: str) -> None:
    mod = _module()
    rows = _rows(mod)
    if case == "missing":
        rows.pop(0)
    elif case == "active_fixture":
        rows[-1]["state"] = "Running"
    elif case == "production_stopped":
        rows[0]["state"] = "Ready"
    elif case == "occupied":
        rows.append({"name": mod.TARGETS[0], "state": "Disabled", "enabled": False})
    elif case == "duplicate":
        rows.append(dict(rows[0]))
    elif case == "extra":
        rows.append({"name": "Unexpected task", "state": "Disabled", "enabled": False})
    elif case == "wrong_enable_type":
        rows[-1]["enabled"] = 0
    with pytest.raises(mod.I4PreflightError):
        mod.verify_task_rows(rows)


@pytest.mark.parametrize("case", ["missing", "running", "enabled", "duplicate"])
def test_i4_postflight_requires_exact_four_disabled_tasks(case: str) -> None:
    mod = _module()
    rows = _rows(mod, candidates=True)
    if case == "missing":
        rows.pop()
    elif case == "running":
        rows[-1]["state"] = "Running"
    elif case == "enabled":
        rows[-1]["enabled"] = True
    elif case == "duplicate":
        rows.append(dict(rows[-1]))
    with pytest.raises(mod.I4PreflightError):
        mod.verify_task_rows(rows, require_candidates_absent=False)


def test_i4_preflight_contains_no_task_mutation_command() -> None:
    src = _SOURCE.read_text(encoding="utf-8")
    for denied in (
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Set-ScheduledTask",
        "Scheduler().install(",
        "Scheduler().stop(",
        "storage.prepare(",
        "storage.stage(",
        "Remove-Item",
        "Stop-Process",
        "git push",
        "secret_hex",
    ):
        assert denied not in src
    assert "Get-ScheduledTask" in src
    assert "IsUserAnAdmin" in src
    assert "sys.flags.isolated" in src
    assert "sys.dont_write_bytecode" in src
