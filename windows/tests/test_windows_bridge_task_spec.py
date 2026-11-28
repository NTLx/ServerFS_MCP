"""Contract-only W1b tests: no Windows service, registry, Bridge or process is started."""

from __future__ import annotations

import copy
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp import windows_bridge_task_spec as spec

SID = "S-1-5-21-100-200-300-1000"
NS = {"t": spec.TASK_XML_NAMESPACE}


@pytest.fixture
def task(tmp_path: Path) -> spec.BridgeTaskSpec:
    return spec.BridgeTaskSpec(
        user_sid=SID,
        task_executable=tmp_path / "pythonw.exe",
        config_path=tmp_path / "private" / "serverfs.toml",
    )


def document(task: spec.BridgeTaskSpec) -> tuple[ET.Element, dict]:
    xml = spec.build_task_xml(task, start_boundary="2026-11-10T22:00:00+08:00")
    return ET.fromstring(xml), spec.build_manifest(task, xml)


def encoded(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def test_spec_has_a_distinct_fixed_owner_task_and_immutable_arguments(
    task: spec.BridgeTaskSpec,
) -> None:
    assert spec.TASK_NAME == "ServerFS Agent Bridge"
    assert spec.TASK_NAME not in {"ServerFS Native Tunnel", "ServerFS Cloudflare WorkPC"}
    assert task.arguments() == [
        "-I",
        "-m",
        "serverfs_mcp.windows_bridge_service",
        "_watchdog",
        "--config",
        str(task.config_path),
    ]
    joined = " ".join(task.arguments())
    for forbidden in ("tunnel-client", "cloudflared", "serverfs_mcp.supervisor"):
        assert forbidden not in joined


def test_registration_is_current_user_non_elevated_and_disabled_by_default(
    task: spec.BridgeTaskSpec,
) -> None:
    root, _ = document(task)

    def field(path: str) -> str | None:
        return root.findtext(path, namespaces=NS)

    assert field("./t:RegistrationInfo/t:URI") == "\\ServerFS Agent Bridge"
    assert field("./t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert field("./t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert field("./t:Settings/t:Enabled") == "false"
    assert field("./t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert field("./t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert field("./t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT1M"
    assert field("./t:Settings/t:RestartOnFailure/t:Count") == "255"
    assert field("./t:Actions/t:Exec/t:WorkingDirectory") == str(task.task_executable.parent)
    assert field("./t:Actions/t:Exec/t:WorkingDirectory") != str(task.config_path.parent)


def test_valid_registration_and_manifest_round_trip(task: spec.BridgeTaskSpec) -> None:
    xml = spec.build_task_xml(task, start_boundary="2026-11-10T22:00:00+08:00")
    manifest = spec.build_manifest(task, xml)
    assert spec.validate_manifest(manifest) == (True, "")
    assert spec.validate_registered_task(xml.decode("utf-16"), manifest) == (True, "")
    assert len(manifest["task_xml_sha256"]) == 64
    assert manifest["component"] == "agent-bridge"
    assert manifest["bridge_executable"] == str(task.bridge_executable())
    assert manifest["bridge_executable"] != manifest["task_executable"]
    assert "CONTROL_PLANE_API_KEY" not in str(manifest)
    assert "MCP_EXTRA_HEADERS" not in str(manifest)


def test_authored_xml_digest_blocks_single_byte_tamper(task: spec.BridgeTaskSpec) -> None:
    xml = spec.build_task_xml(task, start_boundary="2026-11-10T22:00:00+08:00")
    manifest = spec.build_manifest(task, xml)
    assert spec.validate_authored_task_xml(xml, manifest) == (True, "")
    assert spec.validate_authored_task_xml(xml + b"extra", manifest)[0] is False
    fake_digest = copy.deepcopy(manifest)
    fake_digest["task_xml_sha256"] = "a" * 64
    assert spec.validate_manifest(fake_digest) == (True, "")
    assert spec.validate_authored_task_xml(xml, fake_digest)[0] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("component", "openai"),
        ("task_name", "ServerFS Native Tunnel"),
        ("task_uri", "\\ServerFS Native Tunnel"),
        ("schema_version", 3),
        ("task_arguments", ["-m", "serverfs_mcp.supervisor"]),
        ("config_path", "serverfs.toml"),
        ("task_executable", "powershell.exe"),
        ("bridge_executable", "C:/Injected/python.exe"),
        ("task_xml_sha256", "not-a-digest"),
    ],
)
def test_manifest_rejects_foreign_or_untrusted_fields(
    task: spec.BridgeTaskSpec, field: str, value: object
) -> None:
    _, original = document(task)
    tampered = copy.deepcopy(original)
    tampered[field] = value
    assert spec.validate_manifest(tampered)[0] is False


def test_manifest_rejects_unknown_authority_field(task: spec.BridgeTaskSpec) -> None:
    _, manifest = document(task)
    manifest["operator_argv"] = "cmd.exe /c arbitrary"
    assert spec.validate_manifest(manifest)[0] is False


@pytest.mark.parametrize("sid", ["", "DOMAIN\\user", "S-1-x-3", "S-1-5-a"])
def test_bad_sid_refused_before_task_creation(task: spec.BridgeTaskSpec, sid: str) -> None:
    bad = spec.BridgeTaskSpec(sid, task.task_executable, task.config_path)
    with pytest.raises(spec.BridgeTaskContractError):
        spec.build_task_xml(bad)


def test_non_pythonw_executable_or_relative_config_refused(task: spec.BridgeTaskSpec) -> None:
    bad_exe = spec.BridgeTaskSpec(SID, task.task_executable.with_name("cmd.exe"), task.config_path)
    bad_path = spec.BridgeTaskSpec(SID, task.task_executable, Path("relative.toml"))
    for candidate in (bad_exe, bad_path):
        with pytest.raises(spec.BridgeTaskContractError):
            spec.build_task_xml(candidate)


def test_explicitly_enabled_xml_is_possible_without_implicit_autostart(
    task: spec.BridgeTaskSpec,
) -> None:
    enabled = spec.BridgeTaskSpec(SID, task.task_executable, task.config_path, enabled=True)
    root = ET.fromstring(spec.build_task_xml(enabled))
    assert root.findtext("./t:Settings/t:Enabled", namespaces=NS) == "true"


@pytest.mark.parametrize(
    ("element_path", "value"),
    [
        ("./t:RegistrationInfo/t:URI", "\\ServerFS Native Tunnel"),
        ("./t:Principals/t:Principal/t:LogonType", "Password"),
        ("./t:Principals/t:Principal/t:RunLevel", "HighestAvailable"),
        ("./t:Settings/t:MultipleInstancesPolicy", "Parallel"),
        ("./t:Settings/t:ExecutionTimeLimit", "PT1H"),
        ("./t:Triggers/t:LogonTrigger/t:Enabled", "false"),
        ("./t:Triggers/t:TimeTrigger/t:Enabled", "false"),
        ("./t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd", "true"),
        ("./t:Actions/t:Exec/t:Command", "powershell.exe"),
        ("./t:Actions/t:Exec/t:Arguments", subprocess.list2cmdline(["-m", "broken"])),
    ],
)
def test_registration_refuses_changed_component_or_privilege(
    task: spec.BridgeTaskSpec, element_path: str, value: str
) -> None:
    root, manifest = document(task)
    element = root.find(element_path, NS)
    assert element is not None
    element.text = value
    assert spec.validate_registered_task(encoded(root), manifest)[0] is False


def test_registration_refuses_added_action_or_trigger(task: spec.BridgeTaskSpec) -> None:
    root, manifest = document(task)
    actions = root.find("./t:Actions", NS)
    assert actions is not None
    ET.SubElement(actions, f"{{{spec.TASK_XML_NAMESPACE}}}ComHandler")
    assert spec.validate_registered_task(encoded(root), manifest)[0] is False

    root, manifest = document(task)
    triggers = root.find("./t:Triggers", NS)
    assert triggers is not None
    ET.SubElement(triggers, f"{{{spec.TASK_XML_NAMESPACE}}}BootTrigger")
    assert spec.validate_registered_task(encoded(root), manifest)[0] is False


def test_registered_xml_never_contains_secrets_or_connector_ownership(
    task: spec.BridgeTaskSpec,
) -> None:
    rendered = spec.build_task_xml(task)
    text = rendered.decode("utf-16")
    for forbidden in (
        "CONTROL_PLANE_API_KEY",
        "cloudflared",
        "tunnel-client",
        "serverfs_mcp.supervisor",
        "LocalSystem",
        "HighestAvailable",
    ):
        assert forbidden not in text
