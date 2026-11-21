"""Pure separate connector Task Scheduler identity/manifest validation."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from serverfs_mcp import windows_connector_task_spec as contract
from serverfs_mcp.windows_component_task import NAMESPACE

SID = "S-1-5-21-100-200-300-1000"
NS = {"t": NAMESPACE}


@pytest.fixture(params=("cloudflare", "openai"))
def spec(request, tmp_path: Path) -> contract.ConnectorTaskSpec:
    kind = request.param
    return contract.ConnectorTaskSpec(
        kind=kind,
        sid=SID,
        pythonw=tmp_path / kind / "Scripts" / "pythonw.exe",
        policy_file=tmp_path / kind / "policy.json",
    )


def bundle(spec):
    xml = contract.build_task_xml(spec, boundary="2026-11-11T08:00:00+08:00")
    return xml, contract.build_manifest(spec, xml, b'{"kind":"test"}')


def test_connectors_have_distinct_disabled_tasks_and_fixed_module(spec) -> None:
    xml, manifest = bundle(spec)
    root = ET.fromstring(xml)
    assert spec.kind in ("openai", "cloudflare")
    assert root.findtext("./t:Settings/t:Enabled", namespaces=NS) == "false"
    assert root.findtext("./t:RegistrationInfo/t:URI", namespaces=NS) == (
        "\\" + contract.TASK_NAMES[spec.kind]
    )
    assert "serverfs_mcp.windows_connector_service" in str(spec.arguments())
    assert spec.arguments()[:4] == ("-I", "-m", "serverfs_mcp.windows_connector_service", "_run")
    assert "supervisor" not in str(manifest)
    assert "serverfs_mcp.windows_core_service" not in str(manifest)
    assert "serverfs_mcp.windows_bridge_service" not in str(manifest)


def test_connector_task_manifest_authored_and_os_normalized_contract(spec) -> None:
    xml, manifest = bundle(spec)
    assert contract.validate_manifest(manifest) == (True, "")
    assert contract.validate_authored_task(xml, manifest) == (True, "")
    assert contract.validate_registered_task(xml.decode("utf-16"), manifest) == (True, "")


@pytest.mark.parametrize(
    "field,value",
    [
        ("component", "mcp-core"),
        ("kind", "custom"),
        ("sid", "foreign"),
        ("pythonw", "python.exe"),
        ("policy_file", "relative.json"),
        ("task_name", "ServerFS Native Tunnel"),
        ("schema_version", True),
        ("task_sha256", "invalid"),
        ("policy_sha256", "invalid"),
        ("arguments", ["python", "-c", "untrusted"]),
    ],
)
def test_manifest_rejects_tampered_contract(spec, field: str, value: object) -> None:
    _, manifest = bundle(spec)
    altered = copy.deepcopy(manifest)
    altered[field] = value
    assert contract.validate_manifest(altered)[0] is False


def test_authored_xml_digest_refuses_modified_binary(spec) -> None:
    xml, manifest = bundle(spec)
    assert contract.validate_authored_task(xml + b"x", manifest)[0] is False
    manifest["policy_sha256"] = "0" * 64
    # The policy SHA is valid in form; policy bytes are checked by the owner.
    assert contract.validate_manifest(manifest) == (True, "")


@pytest.mark.parametrize(
    "field,value",
    [
        ("./t:Principals/t:Principal/t:RunLevel", "HighestAvailable"),
        ("./t:Principals/t:Principal/t:LogonType", "Password"),
        ("./t:Actions/t:Exec/t:Command", "cmd.exe"),
        ("./t:Settings/t:ExecutionTimeLimit", "PT5M"),
        ("./t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", "PT9M"),
    ],
)
def test_registered_task_refuses_drift(spec, field: str, value: str) -> None:
    xml, manifest = bundle(spec)
    root = ET.fromstring(xml)
    node = root.find(field, NS)
    assert node is not None
    node.text = value
    altered = ET.tostring(root, encoding="unicode")
    assert contract.validate_registered_task(altered, manifest)[0] is False


def test_registered_task_rejects_foreign_action(spec) -> None:
    xml, manifest = bundle(spec)
    root = ET.fromstring(xml)
    actions = root.find("./t:Actions", NS)
    assert actions is not None
    ET.SubElement(actions, f"{{{NAMESPACE}}}ComHandler")
    assert (
        contract.validate_registered_task(ET.tostring(root, encoding="unicode"), manifest)[0]
        is False
    )


def test_invalid_task_names_and_interpreters_refused(spec) -> None:
    with pytest.raises(contract.ConnectorTaskError):
        replace(spec, kind="new").validated()
    with pytest.raises(contract.ConnectorTaskError):
        replace(spec, pythonw=Path("cmd.exe")).validated()
