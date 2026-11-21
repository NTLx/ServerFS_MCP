"""Synthetic Scheduler registration and lifecycle isolation for named Connectors.

Uses real v25 Task XML/validation and a strict in-memory schtasks runner.
Never registers a Windows task, starts a tunnel, or reads real credentials.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp import windows_private_artifacts
from serverfs_mcp.windows_component_task import NAMESPACE, validate_registered
from serverfs_mcp.windows_v25 import readiness, storage, tasks
from serverfs_mcp.windows_v25.spec import Manifest

SID = "S-1-5-21-111-222-333-1001"


def connector(tmp_path: Path, connector_id: str) -> Manifest:
    root = tmp_path / "instances" / "fixture"
    return Manifest.parse(
        {
            "schema": 2,
            "component": "cloudflare",
            "instance_id": "fixture",
            "connector_id": connector_id,
            "sid": SID,
            "pythonw": str(root / "envs/cloudflare/Scripts/pythonw.exe"),
            "settings": {
                "cloudflared": str(root / "bin/cloudflared.exe"),
                "token_file": str(root / "private" / (connector_id + ".token")),
                "port": 18879,
                "hostname": "core.example.test",
            },
        }
    )


class SimulatedScheduler:
    def __init__(self, manifests: tuple[Manifest, ...]) -> None:
        self.targets = {item.task_name() for item in manifests}
        self.xml: dict[str, bytes] = {}
        self.enabled: dict[str, bool] = {}
        self.running: dict[str, bool] = {}
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(argv)
        if argv[0] == "powershell.exe":
            matches = [name for name in self.targets if f"-TaskName '{name}'" in argv[-1]]
            assert len(matches) == 1
            name = matches[0]
            value = (
                "Running" if self.running[name] else "Ready" if self.enabled[name] else "Disabled"
            )
            return subprocess.CompletedProcess(argv, 0, value.encode("ascii"))
        op, name = argv[1], argv[3]
        assert argv[2] == "/TN" and name in self.targets
        if op == "/Query":
            return (
                subprocess.CompletedProcess(argv, 0, self.xml[name])
                if name in self.xml
                else subprocess.CompletedProcess(argv, 0x80070002, b"")
            )
        if op == "/Create":
            assert name not in self.xml and argv[4] == "/XML"
            self.xml[name] = Path(argv[5]).read_bytes()
            self.enabled[name] = False
            self.running[name] = False
        elif op == "/Change":
            assert name in self.xml and argv[4] in ("/ENABLE", "/DISABLE")
            self.enabled[name] = argv[4] == "/ENABLE"
            # The OS exports the *current* Enabled flag in /Query /XML;
            # it does not keep serving the original disabled staging XML.
            tree = ET.fromstring(self.xml[name])
            enabled = tree.find(f"./{{{NAMESPACE}}}Settings/{{{NAMESPACE}}}Enabled")
            assert enabled is not None
            enabled.text = "true" if self.enabled[name] else "false"
            self.xml[name] = ET.tostring(tree, encoding="utf-16")
        elif op == "/Run":
            assert name in self.xml and self.enabled[name]
            self.running[name] = True
        elif op == "/End":
            assert self.running[name]
            self.running[name] = False
        elif op == "/Delete":
            assert name in self.xml and argv[4] == "/F"
            del self.xml[name]
            del self.enabled[name]
            del self.running[name]
        else:
            raise AssertionError("unknown Scheduler operation")
        return subprocess.CompletedProcess(argv, 0, b"")


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "_schtasks_executable", lambda: Path("schtasks.exe"))
    a, b = connector(tmp_path, "edge-a"), connector(tmp_path, "edge-b")
    runner = SimulatedScheduler((a, b))

    def private_home(component: str, instance: str, connector_id: str) -> Path:
        assert component == "cloudflare" and instance == "fixture"
        assert connector_id in ("edge-a", "edge-b")
        target = (
            tmp_path / "instances" / instance / "services" / component / "connectors" / connector_id
        )
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(tasks, "_platform", lambda: None)
    monkeypatch.setattr(storage, "home", private_home)
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "pinned_pythonw", lambda component, instance: Path(a.pythonw))
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda path, sid: None)
    monkeypatch.setattr(readiness, "_current_identity", lambda manifest: None)
    monkeypatch.setattr(
        readiness,
        "status_readiness",
        lambda manifest, *, task_running: "owner_alive" if task_running else "offline",
    )
    return a, b, runner, tasks.Scheduler(runner=runner)


def test_named_connector_registered_disabled_independently_then_uninstalled(fixture) -> None:
    a, b, runner, scheduler = fixture
    for item in (a, b):
        status = scheduler.install(item)
        assert status["task_name"] == item.task_name()
        assert status["enabled"] is False
        assert status["state"] == "Disabled"
        assert status["running"] is False
        valid, reason = validate_registered(
            tasks._decode_xml(runner.xml[item.task_name()]), tasks._task_spec(item)
        )
        assert valid, reason
        assert tasks._task_spec(item).arguments[-3:] == ("cloudflare", "fixture", item.connector_id)
    name_a, name_b = a.task_name(), b.task_name()
    assert set(runner.xml) == {name_a, name_b}
    before_b = runner.xml[name_b]
    assert scheduler.start(a)["running"] is True
    assert runner.enabled[name_b] is False and runner.running[name_b] is False
    assert scheduler.stop(a)["state"] == "Disabled"
    assert runner.xml[name_b] == before_b
    assert scheduler.uninstall(a)["registered"] is False
    assert scheduler.status(b)["state"] == "Disabled"
    assert scheduler.uninstall(b)["registered"] is False
    assert not runner.xml
    assert all("ServerFS Native Tunnel" not in " ".join(command) for command in runner.commands)


def test_swapped_named_task_definition_rejected_without_peer_mutation(fixture) -> None:
    a, b, runner, scheduler = fixture
    scheduler.install(a)
    scheduler.install(b)
    peer_xml = runner.xml[b.task_name()]
    runner.xml[a.task_name()] = peer_xml
    with pytest.raises(tasks.TaskError, match="owner contract"):
        scheduler.status(a)
    with pytest.raises(tasks.TaskError, match="owner contract"):
        scheduler.start(a)
    assert runner.xml[b.task_name()] == peer_xml
    assert runner.enabled[b.task_name()] is False
    assert runner.running[b.task_name()] is False
    assert scheduler.status(b)["state"] == "Disabled"
