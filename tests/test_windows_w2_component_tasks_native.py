"""Opt-in W2 native disabled registration of all four actual v0.25 task contracts.

This does NOT start any component, change legacy production tasks, or read
credentials. Each expected new task name must initially be ABSENT. Only a
verified, disabled task created in the current test can be removed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from serverfs_mcp import (
    windows_bridge_task_spec as bridge,
)
from serverfs_mcp import (
    windows_connector_task_spec as connector,
)
from serverfs_mcp import (
    windows_core_task_spec as core,
)
from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_component_task import NAMESPACE, ComponentTask
from serverfs_mcp.windows_service import (
    _decode_process_output,
    _identity_matches_sid,
    _schtasks_executable,
)

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_COMPONENT_TASKS") != "1",
        reason="W2 independent component task fixture requires explicit opt-in",
    ),
]

_NS = {"t": NAMESPACE}
_NOT_FOUND = frozenset({0x80070002, 0x80070003})


def _call(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_schtasks_executable()), *args],
        capture_output=True,
        check=False,
        timeout=25,
    )


def _registered(name: str) -> str | None:
    result = _call("/Query", "/TN", name, "/XML", "/HResult")
    if result.returncode == 0:
        return _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
    if result.returncode & 0xFFFFFFFF in _NOT_FOUND:
        return None
    pytest.fail("component fixture Task Scheduler query was refused")


def _field(root: ET.Element, path: str) -> str:
    return root.findtext(path, default="", namespaces=_NS)


def _safe_cleanup(xml: str, task: ComponentTask) -> bool:
    root = ET.fromstring(xml)
    return (
        _field(root, "./t:RegistrationInfo/t:URI") == task.uri
        and _identity_matches_sid(_field(root, "./t:Principals/t:Principal/t:UserId"), task.sid)
        and os.path.normcase(_field(root, "./t:Actions/t:Exec/t:Command"))
        == os.path.normcase(str(task.executable))
        and _field(root, "./t:Actions/t:Exec/t:Arguments")
        == subprocess.list2cmdline(task.arguments)
        and _field(root, "./t:Settings/t:Enabled") == "false"
    )


def _candidate(
    kind: str, tmp_path: Path
) -> tuple[ComponentTask, bytes, Callable[[str], tuple[bool, str]]]:
    sid = current_user_sid()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file(), "W2 host lacks pythonw.exe"
    if kind == "bridge":
        spec = bridge.BridgeTaskSpec(sid, pythonw, tmp_path / "bridge.toml")
        source = bridge.build_task_xml(spec)
        manifest = bridge.build_manifest(spec, source)
        task = ComponentTask(bridge.TASK_NAME, sid, pythonw, tuple(spec.arguments()), False)
        validator = bridge.validate_registered_task
    elif kind == "core":
        spec = core.CoreTaskSpec(
            sid,
            pythonw,
            tmp_path / "core.toml",
            tmp_path / "core-auth.env",
            49876,
            "core.w2.test",
        )
        source = core.build_task_xml(spec)
        manifest = core.manifest_for(spec, source)
        task = spec.task()
        validator = core.validate_registered_xml
    else:
        spec = connector.ConnectorTaskSpec(kind, sid, pythonw, tmp_path / f"{kind}.json")
        source = connector.build_task_xml(spec)
        manifest = connector.build_manifest(spec, source, b"synthetic fixture")
        task = spec.task()
        validator = connector.validate_registered_task
    return task, source, partial(validator, manifest=manifest)


@pytest.mark.parametrize("kind", ("bridge", "core", "openai", "cloudflare"))
def test_each_real_component_task_round_trips_disabled_in_scheduler(
    kind: str, tmp_path: Path
) -> None:
    task, xml, validator = _candidate(kind, tmp_path)
    assert _registered(task.name) is None, (
        "W2 component task name is already registered: do not overwrite"
    )
    authored = tmp_path / f"{kind}.xml"
    authored.write_bytes(xml)
    created = False
    try:
        result = _call("/Create", "/TN", task.name, "/XML", str(authored))
        assert result.returncode == 0, "W2 component disabled task registration failed"
        created = True
        current = _registered(task.name)
        assert current is not None, "W2 component task is missing after registration"
        valid, reason = validator(current)
        assert valid, f"W2 normalized task contract mismatch: {reason}"
        assert _field(ET.fromstring(current), "./t:Settings/t:Enabled") == "false"
    finally:
        observed = _registered(task.name)
        if observed is not None:
            if created and _safe_cleanup(observed, task):
                removed = _call("/Delete", "/TN", task.name, "/F")
                assert removed.returncode == 0, "W2 disabled component cleanup failed"
                assert _registered(task.name) is None
            else:
                pytest.fail("W2 component task remains; ownership unclear, not removed")
