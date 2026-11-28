"""Opt-in native W2 Task Scheduler XML fixture: disabled, unique, ephemeral.

Never registers ServerFS production component names or starts the task.
The environment gate avoids changing Windows tasks in normal unit tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_component_task import (
    NAMESPACE,
    ComponentTask,
    build_xml,
    validate_registered,
)
from serverfs_mcp.windows_service import _decode_process_output, _schtasks_executable

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W2_SCHEDULER_FIXTURE") != "1",
        reason="explicit isolated Task Scheduler fixture opt-in required",
    ),
]

_TASK_NOT_FOUND = {0x80070002, 0x80070003}
_NS = {"t": NAMESPACE}


def _disabled(xml: str) -> bool:
    root = ET.fromstring(xml)
    return root.findtext("./t:Settings/t:Enabled", namespaces=_NS) == "false"


def _cleanup_identity(xml: str, spec: ComponentTask) -> bool:
    """Require immutable name, user and safe action before deleting a fixture."""
    root = ET.fromstring(xml)
    from serverfs_mcp.windows_service import _identity_matches_sid

    def get(selector: str) -> str:
        return root.findtext(selector, default="", namespaces=_NS)

    return (
        get("./t:RegistrationInfo/t:URI") == spec.uri
        and _identity_matches_sid(get("./t:Principals/t:Principal/t:UserId"), spec.sid)
        and os.path.normcase(get("./t:Actions/t:Exec/t:Command"))
        == os.path.normcase(str(spec.executable))
        and get("./t:Actions/t:Exec/t:Arguments") == subprocess.list2cmdline(spec.arguments)
        and _disabled(xml)
    )


def _task(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_schtasks_executable()), *args],
        capture_output=True,
        check=False,
        timeout=20,
    )


def _read(task_name: str) -> str | None:
    result = _task("/Query", "/TN", task_name, "/XML", "/HResult")
    if result.returncode == 0:
        return _decode_process_output(result.stdout).lstrip("\ufeff\x00 \t\r\n")
    if (result.returncode & 0xFFFFFFFF) in _TASK_NOT_FOUND:
        return None
    pytest.fail("W2 fixture Task Scheduler query is denied or failed")


def test_isolated_disabled_task_scheduler_xml_round_trip(tmp_path: Path) -> None:
    name = f"ServerFS W2 Fixture {uuid4().hex[:16]}"
    spec = ComponentTask(
        name=name,
        sid=current_user_sid(),
        executable=Path(sys.executable),
        arguments=("-I", "-c", "pass"),  # Never executed: disabled task
        enabled=False,
    )
    if _read(name) is not None:
        pytest.fail("W2 fixture task name already exists")
    xml_path = tmp_path / "w2-fixture.xml"
    xml_path.write_bytes(build_xml(spec))
    created = False
    try:
        result = _task("/Create", "/TN", name, "/XML", str(xml_path))
        if result.returncode != 0:
            # Even an errored Create can have registered something. Do not
            # assume there is nothing to clean up without querying again.
            pytest.fail("W2 disabled Task Scheduler registration was refused")
        created = True
        registered = _read(name)
        assert registered is not None, "W2 task disappeared after registration"
        valid, reason = validate_registered(registered, spec)
        assert valid, f"W2 registered XML contract mismatch: {reason}"
        assert _disabled(registered), "W2 fixture was unexpectedly enabled"
    finally:
        current = _read(name)
        if current is not None:
            # Cleanup requires invariant task name/SID/action and disabled
            # state even if Scheduler normalized other authored XML settings.
            if created and _cleanup_identity(current, spec):
                removed = _task("/Delete", "/TN", name, "/F")
                assert removed.returncode == 0, "W2 fixture Task Scheduler cleanup failed"
                assert _read(name) is None, "W2 fixture task persists after cleanup"
            else:
                pytest.fail("W2 fixture task remains: cleanup refused because ownership unclear")
