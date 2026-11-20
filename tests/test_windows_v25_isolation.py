"""Windows v25 I1: collision and task status isolation (synthetic only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25 import cli, storage, tasks
from serverfs_mcp.windows_v25.isolation import (
    InstanceCollisionError,
    assert_core_isolation,
)
from serverfs_mcp.windows_v25.spec import Manifest


def profile(tmp_path: Path, name: str, port: int = 19031) -> Manifest:
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": name,
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / name / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(tmp_path / name / "policy/serverfs.toml"),
                "auth_file": str(tmp_path / name / "secret/core.env"),
                "port": port,
                "hostname": "core.example.net",
            },
        }
    )


def test_separate_core_profiles_and_duplicate_same_manifest(tmp_path: Path) -> None:
    current = profile(tmp_path, "stage", 19031)
    different = profile(tmp_path, "qa", 19032)
    assert_core_isolation(current, (different, current))


@pytest.mark.parametrize("changed", ["port", "hostname", "auth_file", "pythonw"])
def test_same_core_instance_conflicting_manifest_is_rejected(tmp_path: Path, changed: str) -> None:
    """A same-ID peer may be exactly self, but not a different owner contract."""
    candidate = profile(tmp_path, "stage", 19031)
    raw = candidate.to_dict()
    if changed == "port":
        raw["settings"]["port"] = 19032
    elif changed == "hostname":
        raw["settings"]["hostname"] = "different.example.net"
    elif changed == "auth_file":
        raw["settings"]["auth_file"] = str(tmp_path / "alternate" / "core.env")
    else:
        raw["pythonw"] = str(tmp_path / "alternate" / "pythonw.exe")
    other = Manifest.parse(raw)
    assert other.instance_id == candidate.instance_id
    assert other != candidate
    with pytest.raises(InstanceCollisionError, match="Core instance identity"):
        assert_core_isolation(candidate, [other])


def test_duplicate_loopback_port_is_rejected(tmp_path: Path) -> None:
    current = profile(tmp_path, "stage", 19031)
    other = profile(tmp_path, "qa", 19031)
    with pytest.raises(InstanceCollisionError, match="port"):
        assert_core_isolation(current, [other])


@pytest.mark.parametrize("setting", ["config", "auth_file"])
def test_private_credentials_or_configuration_cannot_be_shared(
    tmp_path: Path, setting: str
) -> None:
    current = profile(tmp_path, "stage", 19031)
    other = profile(tmp_path, "qa", 19032)
    data = other.to_dict()
    data["settings"][setting] = str(current.settings[setting]).upper()
    same = Manifest.parse(data)
    with pytest.raises(InstanceCollisionError, match="private inputs"):
        assert_core_isolation(current, [same])


def test_interpreter_path_collision_is_rejected(tmp_path: Path) -> None:
    current = profile(tmp_path, "stage", 19031)
    other = profile(tmp_path, "qa", 19032)
    data = other.to_dict()
    data["pythonw"] = current.pythonw
    with pytest.raises(InstanceCollisionError, match="interpreter"):
        assert_core_isolation(current, [Manifest.parse(data)])


def test_status_reports_selected_task_not_default(tmp_path: Path, monkeypatch) -> None:
    candidate = profile(tmp_path, "stage")
    scheduler = tasks.Scheduler(runner=lambda command: pytest.fail("no OS calls"))
    monkeypatch.setattr(
        scheduler,
        "_record",
        lambda manifest, missing_ok=False: tasks.TaskRecord("Disabled", False),
    )
    state = scheduler.status(candidate)
    assert state["task_name"] == "ServerFS v25 stage Core"
    assert state["readiness"] == "offline"


def test_stop_named_core_preserves_task_identity(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import readiness

    candidate = profile(tmp_path, "stage")
    scheduler = tasks.Scheduler(runner=lambda command: pytest.fail("no OS calls"))
    monkeypatch.setattr(tasks, "_platform", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_record",
        lambda manifest, missing_ok=False: tasks.TaskRecord("Disabled", False),
    )
    monkeypatch.setattr(readiness, "_current_identity", lambda manifest: None)
    result = scheduler.stop(candidate)
    assert result["task_name"] == candidate.task_name()
    assert result["state"] == "Disabled"


def test_stop_running_named_core_touches_only_its_exact_scheduler_task(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from serverfs_mcp.windows_v25 import readiness

    candidate = profile(tmp_path, "stage")
    foreign = profile(tmp_path, "qa", 19032)
    flags = {"enabled": True, "running": True}
    calls: list[tuple[str, str]] = []
    scheduler = tasks.Scheduler(runner=lambda argv: pytest.fail("no host subprocess"))
    monkeypatch.setattr(tasks, "_platform", lambda: None)
    monkeypatch.setattr(readiness, "_current_identity", lambda manifest: None)

    def record(manifest: Manifest, *, missing_ok: bool = False) -> tasks.TaskRecord:
        assert manifest == candidate
        return tasks.TaskRecord(
            "Running" if flags["running"] else "Disabled",
            flags["enabled"],
        )

    def change(manifest: Manifest, option: str) -> None:
        assert manifest == candidate
        assert option == "/DISABLE"
        calls.append(("disable", manifest.task_name()))
        flags["enabled"] = False

    def schtasks(*args: str) -> subprocess.CompletedProcess[bytes]:
        assert args == ("/End", "/TN", candidate.task_name())
        calls.append(("end", args[-1]))
        flags["running"] = False
        return subprocess.CompletedProcess(args, 0, b"")

    monkeypatch.setattr(scheduler, "_record", record)
    monkeypatch.setattr(scheduler, "_change", change)
    monkeypatch.setattr(scheduler, "_schtasks", schtasks)
    status = scheduler.stop(candidate)
    assert status["task_name"] == candidate.task_name()
    assert calls == [
        ("disable", candidate.task_name()),
        ("end", candidate.task_name()),
    ]
    assert all(name != foreign.task_name() for _, name in calls)


def test_non_core_inventory_entry_fails_closed(tmp_path: Path) -> None:
    current = profile(tmp_path, "stage")
    raw = {
        "schema": 1,
        "component": "bridge",
        "sid": current.sid,
        "pythonw": str(tmp_path / "envs/bridge/Scripts/pythonw.exe"),
        "settings": {
            "config": str(tmp_path / "policy/serverfs.toml"),
            "bridge_python": str(tmp_path / "envs/bridge/Scripts/python.exe"),
        },
    }
    with pytest.raises(InstanceCollisionError, match="inventory is invalid"):
        assert_core_isolation(current, [Manifest.parse(raw)])


def test_private_inventory_checks_installed_core_manifests(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import native_lifecycle

    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "S-1-5-21-111-222-333-1001")
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: appdata)
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)

    a = profile(tmp_path, "stage", 19031)
    b = profile(tmp_path, "qa", 19032)
    for value in (a, b):
        raw = value.to_dict()
        raw["pythonw"] = str(storage.pinned_pythonw("core", value.instance_id))
        path = storage.home("core", value.instance_id) / "service.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
    result = storage.core_inventory()
    assert {value.instance_id for value in result} == {"stage", "qa"}


def test_inventory_rejects_invalid_instance_folder(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import native_lifecycle

    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: appdata)
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)
    base = storage.root() / "instances"
    (base / "_bad").mkdir(parents=True)
    with pytest.raises(storage.StorageError, match="name is invalid"):
        storage.core_inventory()


def test_cli_checks_peers_only_for_nondefault_core(tmp_path: Path, monkeypatch) -> None:
    candidate = profile(tmp_path, "stage")
    monkeypatch.setattr(storage, "core_inventory", lambda: (profile(tmp_path, "qa"),))
    with pytest.raises(InstanceCollisionError, match="port"):
        cli._assert_instance_isolation(candidate)
    monkeypatch.setattr(storage, "core_inventory", lambda: pytest.fail("not called"))
    raw = candidate.to_dict()
    raw["schema"] = 1
    del raw["instance_id"]
    legacy = Manifest.parse(raw)
    cli._assert_instance_isolation(legacy)
