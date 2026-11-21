"""Pure Core Task Scheduler contract tests; no scheduled task is created."""

from __future__ import annotations

import copy
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from serverfs_mcp import windows_core_task_spec as core
from serverfs_mcp.windows_component_task import NAMESPACE

SID = "S-1-5-21-100-200-300-1000"
NS = {"t": NAMESPACE}


@pytest.fixture
def task(tmp_path: Path) -> core.CoreTaskSpec:
    return core.CoreTaskSpec(
        sid=SID,
        pythonw=tmp_path / "core-env" / "Scripts" / "pythonw.exe",
        config_path=tmp_path / "private" / "serverfs.toml",
        auth_file=tmp_path / "private" / "auth.env",
        port=19876,
        hostname="core.example.test",
    )


def bundle(task: core.CoreTaskSpec) -> tuple[bytes, dict]:
    xml = core.build_task_xml(task, boundary="2026-11-11T10:00:00+08:00")
    return xml, core.manifest_for(task, xml)


def test_core_task_is_its_own_disabled_current_user_task(task: core.CoreTaskSpec) -> None:
    xml, manifest = bundle(task)
    root = ET.fromstring(xml)
    assert core.TASK_NAME == "ServerFS MCP Core"
    assert core.TASK_NAME not in ("ServerFS Agent Bridge", "ServerFS Native Tunnel")
    assert root.findtext("./t:Settings/t:Enabled", namespaces=NS) == "false"
    assert root.findtext("./t:Principals/t:Principal/t:RunLevel", namespaces=NS) == (
        "LeastPrivilege"
    )
    assert root.findtext("./t:Principals/t:Principal/t:LogonType", namespaces=NS) == (
        "InteractiveToken"
    )
    assert root.findtext("./t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert manifest["component"] == "mcp-core"
    assert manifest["task_name"] == core.TASK_NAME


def test_core_action_is_direct_http_and_has_no_supervisor(task: core.CoreTaskSpec) -> None:
    xml, manifest = bundle(task)
    root = ET.fromstring(xml)
    argv = task.arguments()
    assert argv[:4] == ("-I", "-m", "serverfs_mcp.windows_core_service", "_run")
    assert "--auth-file" in argv
    assert "private-secret" not in str(manifest)
    assert root.findtext("./t:Actions/t:Exec/t:Arguments", namespaces=NS) == (
        subprocess.list2cmdline(argv)
    )
    assert root.findtext("./t:Actions/t:Exec/t:WorkingDirectory", namespaces=NS) == (
        str(task.pythonw.parent)
    )
    for forbidden in ("supervisor", "cloudflared", "tunnel-client", "CONTROL_PLANE"):
        assert forbidden not in str(manifest)


def test_core_manifest_xml_and_registered_validation(task: core.CoreTaskSpec) -> None:
    xml, manifest = bundle(task)
    assert core.validate_manifest(manifest) == (True, "")
    assert core.validate_authored_xml(xml, manifest) == (True, "")
    assert core.validate_registered_xml(xml.decode("utf-16"), manifest) == (True, "")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_name", "ServerFS Agent Bridge"),
        ("component", "connector"),
        ("port", 0),
        ("sid", "DOMAIN\\name"),
        ("pythonw", "cmd.exe"),
        ("hostname", "*.example.test"),
        ("xml_sha256", "fake"),
        ("schema_version", 2),
        ("schema_version", True),
        ("arguments", ["-m", "serverfs_mcp.supervisor"]),
    ],
)
def test_manifest_rejects_tampered_identity(
    task: core.CoreTaskSpec, field: str, value: object
) -> None:
    _, manifest = bundle(task)
    changed = copy.deepcopy(manifest)
    changed[field] = value
    assert core.validate_manifest(changed)[0] is False


def test_manifest_rejects_unknown_fields(task: core.CoreTaskSpec) -> None:
    _, manifest = bundle(task)
    manifest["command_override"] = "cmd /c"
    assert core.validate_manifest(manifest)[0] is False


def test_authored_hash_refuses_byte_tampering(task: core.CoreTaskSpec) -> None:
    xml, manifest = bundle(task)
    assert core.validate_authored_xml(xml + b"extra", manifest)[0] is False
    manifest["xml_sha256"] = "0" * 64
    assert core.validate_authored_xml(xml, manifest)[0] is False


@pytest.mark.parametrize(
    ("path", "new_value"),
    [
        ("./t:RegistrationInfo/t:URI", "\\ServerFS Native Tunnel"),
        ("./t:Principals/t:Principal/t:RunLevel", "HighestAvailable"),
        ("./t:Principals/t:Principal/t:LogonType", "Password"),
        ("./t:Settings/t:MultipleInstancesPolicy", "Parallel"),
        ("./t:Actions/t:Exec/t:Command", "powershell.exe"),
        ("./t:Actions/t:Exec/t:Arguments", "arbitrary command"),
        ("./t:Settings/t:ExecutionTimeLimit", "PT10S"),
        ("./t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", "PT5M"),
    ],
)
def test_registered_task_refuses_policy_drift(
    task: core.CoreTaskSpec, path: str, new_value: str
) -> None:
    xml, manifest = bundle(task)
    root = ET.fromstring(xml)
    element = root.find(path, NS)
    assert element is not None
    element.text = new_value
    assert core.validate_registered_xml(ET.tostring(root, encoding="unicode"), manifest)[0] is False


def test_foreign_action_or_trigger_refused(task: core.CoreTaskSpec) -> None:
    xml, manifest = bundle(task)
    root = ET.fromstring(xml)
    actions = root.find("./t:Actions", NS)
    assert actions is not None
    ET.SubElement(actions, f"{{{NAMESPACE}}}ComHandler")
    assert core.validate_registered_xml(ET.tostring(root, encoding="unicode"), manifest)[0] is False
    root = ET.fromstring(xml)
    triggers = root.find("./t:Triggers", NS)
    assert triggers is not None
    ET.SubElement(triggers, f"{{{NAMESPACE}}}BootTrigger")
    assert core.validate_registered_xml(ET.tostring(root, encoding="unicode"), manifest)[0] is False


def test_core_task_refuses_invalid_interpreter_or_port(task: core.CoreTaskSpec) -> None:
    with pytest.raises(core.CoreTaskSpecError):
        core.build_task_xml(replace(task, pythonw=task.pythonw.with_name("cmd.exe")))
    with pytest.raises(core.CoreTaskSpecError):
        core.build_task_xml(replace(task, port=0))
    with pytest.raises(core.CoreTaskSpecError):
        core.build_task_xml(replace(task, port=True))
    with pytest.raises(core.CoreTaskSpecError):
        core.build_task_xml(replace(task, hostname="evil.example,*.example"))
