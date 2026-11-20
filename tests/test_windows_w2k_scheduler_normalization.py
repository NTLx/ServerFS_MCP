"""Pure W2K disposable Scheduler XML diagnostic contract tests.

These tests never register, enable, query, run or delete a Windows task.
"""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp.windows_component_task import NAMESPACE, ComponentTask, build_xml


def _probe():
    path = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w2k-scheduler-normalization-probe.py"
    )
    spec = importlib.util.spec_from_file_location("w2k_xml_probe_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_synthetic_task_action_cannot_run_and_triggers_are_inert(
    tmp_path: Path,
) -> None:
    probe = _probe()
    fake_action = tmp_path / "nonexistent-test-action.exe"
    assert not fake_action.exists()
    task = ComponentTask(
        name="ServerFS W2K XML Probe TEST_ONLY",
        sid="S-1-5-21-100-200-300-1000",
        executable=fake_action,
        arguments=(),
        enabled=False,
    )
    xml = probe._fixed_task_xml_with_no_active_logon(task, build_xml, NAMESPACE)
    root = ET.fromstring(xml)
    ns = {"t": NAMESPACE}

    def value(query: str) -> str | None:
        node = root.find(query, ns)
        return None if node is None else node.text

    assert value("./t:Triggers/t:LogonTrigger/t:Enabled") == "false"
    assert value("./t:Triggers/t:TimeTrigger/t:StartBoundary") is not None
    assert value("./t:Triggers/t:TimeTrigger/t:StartBoundary").startswith("2099-")
    assert value("./t:Settings/t:Enabled") == "false"
    assert value("./t:Actions/t:Exec/t:Command") == str(fake_action)
    assert len(root.findall("./t:Actions/t:Exec", ns)) == 1


def test_only_known_redacted_contract_mismatch_labels_are_accepted() -> None:
    probe = _probe()
    assert probe._mismatches("", True) == frozenset()
    assert probe._mismatches(
        "component task contract mismatch: logon enabled, restart count", False
    ) == frozenset({"logon enabled", "restart count"})
    with pytest.raises(ValueError, match="unexpected mismatch"):
        probe._mismatches("component task contract mismatch: C:/private/key", False)
    with pytest.raises(ValueError, match="non-contract"):
        probe._mismatches("task XML contains private contents", False)


def test_operator_wrapper_never_targets_real_services_for_mutation() -> None:
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w2k-scheduler-normalization-probe.ps1"
    ).read_text(encoding="utf-8")
    fixture = (
        Path(__file__).resolve().parents[1]
        / "deployment/windows/v0250-w2k-scheduler-normalization-probe.py"
    ).read_text(encoding="utf-8")
    assert "Assert-ActualTasks" in wrapper
    assert "Get-ScheduledTask" in wrapper
    assert "Disable-ScheduledTask" not in wrapper
    assert "Stop-ScheduledTask" not in wrapper
    assert '"/Run",' not in fixture
    assert '"/Create",' in fixture
    assert '"/Delete",' in fixture
    assert "nonexistent-test-action.exe" in fixture
    assert "2099-01-01T00:00:00+00:00" in fixture
