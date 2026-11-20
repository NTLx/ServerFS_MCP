"""I5 provider-enabled shared Bridge host identity gates, source-only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-provider-shared-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_shared_gate_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(module):
    return [{"name": task, "state": "Disabled", "enabled": False} for task in module.TARGETS]


def test_i5_provider_fixture_uses_fresh_shared_bridge_and_two_core_ids() -> None:
    m = _module()
    assert m.BRIDGE == "i5-shared"
    assert m.CORE_PORTS == {"i5-core-a": 18873, "i5-core-b": 18874}
    assert m.TARGETS == (
        "ServerFS v25 i5-shared Agent Bridge",
        "ServerFS v25 i5-core-a Core",
        "ServerFS v25 i5-core-b Core",
    )
    assert len(set(m.TARGETS)) == 3


def test_i5_preflight_accepts_only_empty_namespace() -> None:
    m = _module()
    m.verify_candidates([], installed=False)
    with pytest.raises(m.I5PreflightError):
        m.verify_candidates(_rows(m), installed=False)


def test_i5_postflight_accepts_exact_three_disabled_tasks() -> None:
    m = _module()
    m.verify_candidates(_rows(m), installed=True)


def test_i5_postflight_allows_only_explicit_optional_disabled_connector_peers() -> None:
    m = _module()
    assert m.CONNECTOR_TARGETS == (
        "ServerFS v25 i5-core-a Cloudflare Connector i5-edge-a",
        "ServerFS v25 i5-core-b Cloudflare Connector i5-edge-b",
        "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-a",
        "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-b",
    )
    for count in range(1, len(m.CONNECTOR_TARGETS) + 1):
        rows = _rows(m) + [
            {"name": name, "state": "Disabled", "enabled": False}
            for name in m.CONNECTOR_TARGETS[:count]
        ]
        m.verify_candidates(rows, installed=True)
        with pytest.raises(m.I5PreflightError):
            m.verify_candidates(rows, installed=False)


def test_i5_postflight_rejects_running_or_foreign_optional_connectors() -> None:
    m = _module()
    row = {"name": m.CONNECTOR_TARGETS[0], "state": "Disabled", "enabled": False}
    m.verify_candidates(_rows(m) + [row], installed=True)
    for patch in (
        {"state": "Running"},
        {"enabled": True},
        {"name": "ServerFS v25 i5-core-a OpenAI Connector i5-edge-a"},
        {"name": "ServerFS v25 i5-core-a Cloudflare Connector foreign"},
    ):
        with pytest.raises(m.I5PreflightError):
            m.verify_candidates(_rows(m) + [{**row, **patch}], installed=True)


@pytest.mark.parametrize(
    "case",
    ["missing", "running", "enabled", "numeric_enable", "duplicate", "foreign", "nondict"],
)
def test_i5_postflight_rejects_inconsistent_tasks(case: str) -> None:
    m = _module()
    rows = _rows(m)
    if case == "missing":
        rows.pop()
    elif case == "running":
        rows[-1]["state"] = "Running"
    elif case == "enabled":
        rows[-1]["enabled"] = True
    elif case == "numeric_enable":
        rows[-1]["enabled"] = 0
    elif case == "duplicate":
        rows.append(dict(rows[0]))
    elif case == "foreign":
        rows[-1]["name"] = "ServerFS v25 i5-foreign Core"
    elif case == "nondict":
        rows[-1] = "not a task"
    with pytest.raises(m.I5PreflightError):
        m.verify_candidates(rows, installed=True)


def test_i5_source_queries_only_protected_and_candidate_scheduler() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)",
        "verify_candidates(candidates(), installed=False)",
        "not root.exists() and not root.is_symlink()",
        "_windows_ipv4_tcp_listeners()",
        "IsUserAnAdmin()",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "I5_NATIVE_PREFLIGHT=PASS",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "storage.prepare(",
        "storage.stage(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Stop-Process",
        "Remove-Item",
        "ANTHROPIC_API_KEY",
        "secret_hex",
        "git push",
    ):
        assert forbidden not in source
