"""Fail-closed native Windows task ownership against safety-policy drift.

Pure XML tests: never register, enable, start, or delete a Windows scheduled task.
The test directly modifies untrusted /Query /XML data, never the original XML.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp.windows_component_task import (
    NAMESPACE,
    ComponentTask,
    build_xml,
    validate_registered,
)
from serverfs_mcp.windows_v25 import storage, tasks
from serverfs_mcp.windows_v25.spec import Manifest

SID = "S-1-5-21-111-222-333-1001"
N = {"t": NAMESPACE}


def _component(tmp_path: Path) -> ComponentTask:
    return ComponentTask(
        name="ServerFS v25 auditfixture Core",
        sid=SID,
        executable=tmp_path / "pythonw.exe",
        arguments=("-I", "-B", "-m", "serverfs_mcp.windows_v25.runtime", "core", "auditfixture"),
        enabled=False,
    )


def _alter(task: ComponentTask, field: str, value: str | None) -> str:
    root = ET.fromstring(build_xml(task))
    settings = root.find("./t:Settings", N)
    assert settings is not None
    item = settings.find("t:" + field, N)
    assert item is not None
    if value is None:
        settings.remove(item)
    else:
        item.text = value
    return ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize(
    ("setting", "altered"),
    [
        ("WakeToRun", "true"),
        ("DisallowStartIfOnBatteries", "true"),
        ("StopIfGoingOnBatteries", "true"),
        ("StartWhenAvailable", "false"),
        ("RunOnlyIfNetworkAvailable", "true"),
        ("RunOnlyIfIdle", "true"),
        ("Hidden", "true"),
        ("AllowStartOnDemand", "false"),
        ("AllowHardTerminate", "false"),
    ],
)
def test_untrusted_scheduler_xml_rejects_unsafe_setting(
    tmp_path: Path, setting: str, altered: str
) -> None:
    spec = _component(tmp_path)
    raw = ET.tostring(ET.fromstring(build_xml(spec)), encoding="unicode")
    assert validate_registered(raw, spec)[0] is True
    valid, reason = validate_registered(_alter(spec, setting, altered), spec)
    assert valid is False
    assert setting in reason


@pytest.mark.parametrize(
    ("setting", "source_default"),
    [
        ("DisallowStartIfOnBatteries", True),
        ("StopIfGoingOnBatteries", True),
        ("StartWhenAvailable", True),
    ],
)
def test_omitted_settings_with_wrong_windows_defaults_are_rejected(
    tmp_path: Path, setting: str, source_default: bool
) -> None:
    # Task Scheduler defaults these three to the opposite of the policy.
    assert source_default
    spec = _component(tmp_path)
    valid, reason = validate_registered(_alter(spec, setting, None), spec)
    assert valid is False
    assert setting in reason


@pytest.mark.parametrize(
    "setting",
    (
        "WakeToRun",
        "RunOnlyIfNetworkAvailable",
        "RunOnlyIfIdle",
        "Hidden",
        "AllowHardTerminate",
        "AllowStartOnDemand",
    ),
)
def test_safe_optional_settings_can_follow_windows_xml_defaults(
    tmp_path: Path, setting: str
) -> None:
    spec = _component(tmp_path)
    assert validate_registered(_alter(spec, setting, None), spec)[0] is True


def test_scheduler_drift_fails_before_mutating_os_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = Manifest.parse(
        {
            "schema": 2,
            "component": "core",
            "instance_id": "auditfixture",
            "sid": SID,
            "pythonw": str(tmp_path / "pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "config.toml"),
                "auth_file": str(tmp_path / "auth.env"),
                "port": 18887,
                "hostname": "audit.example.test",
            },
        }
    )
    monkeypatch.setattr(storage, "pinned_pythonw", lambda *_args: Path(manifest.pythonw))
    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "_schtasks_executable", lambda: Path("schtasks.exe"))
    spec = tasks._task_spec(manifest)
    altered = _alter(spec, "WakeToRun", "true").encode("utf-16")
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert argv[1:] == ["/Query", "/TN", manifest.task_name(), "/XML", "/HResult"]
        return subprocess.CompletedProcess(argv, 0, altered)

    with pytest.raises(tasks.TaskError, match="owner contract"):
        tasks.Scheduler(runner=fake_runner).status(manifest)
    assert len(calls) == 1
