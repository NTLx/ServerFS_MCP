"""Pure synthetic Windows Scheduler transition contract for new v25 owners.

No test registers or changes a real Task Scheduler entry.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp.windows_component_task import NAMESPACE, ComponentTask, build_xml
from serverfs_mcp.windows_v25 import readiness, tasks
from serverfs_mcp.windows_v25.spec import Manifest


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return Manifest.parse(
        {
            "schema": 1,
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "serverfs.toml"),
                "auth_file": str(tmp_path / "private.env"),
                "port": 18767,
                "hostname": "core.example.net",
            },
        }
    )


@pytest.fixture(autouse=True)
def synthetic_pinned_interpreter(monkeypatch, manifest: Manifest) -> None:
    # Real installations derive this fixed path from the private known folder.
    # Synthetic tasks intentionally use their unique temporary venv instead.
    monkeypatch.setattr(tasks.storage, "pinned_pythonw", lambda component: Path(manifest.pythonw))


class FakeScheduler:
    def __init__(self, profile: Manifest) -> None:
        self.profile = profile
        self.present = True
        self.enabled = False
        self.running = False
        self.allow_run = True
        self.foreign_xml = False
        self.omit_enabled_when_true = False
        self.auto_start_on_enable = False
        self.run_raced_with_trigger = False
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        if command[0] == "powershell.exe":
            if not self.present:
                return subprocess.CompletedProcess(command, 1)
            state = "Running" if self.running else ("Ready" if self.enabled else "Disabled")
            return subprocess.CompletedProcess(command, 0, state.encode("ascii"))
        op = command[1]
        if op == "/Query":
            if not self.present:
                return subprocess.CompletedProcess(command, 0x80070002)
            spec = self.profile
            task = ComponentTask(
                name=spec.task_name(),
                sid=spec.sid,
                executable=Path(spec.pythonw),
                arguments=spec.task_argv() if not self.foreign_xml else ("-c", "evil"),
                enabled=self.enabled,
            )
            xml = build_xml(task)
            if self.enabled and self.omit_enabled_when_true:
                root = ET.fromstring(xml)
                settings = root.find(f"./{{{NAMESPACE}}}Settings")
                assert settings is not None
                node = settings.find(f"./{{{NAMESPACE}}}Enabled")
                assert node is not None
                settings.remove(node)
                xml = ET.tostring(root, encoding="utf-16")
            return subprocess.CompletedProcess(command, 0, xml)
        if op == "/Create":
            assert not self.present
            self.present = True
            self.enabled = False
            return subprocess.CompletedProcess(command, 0)
        if op == "/Change":
            assert command[4] in ("/ENABLE", "/DISABLE")
            self.enabled = command[4] == "/ENABLE"
            if self.enabled and self.auto_start_on_enable:
                self.running = True
            return subprocess.CompletedProcess(command, 0)
        if op == "/Run":
            if self.run_raced_with_trigger:
                self.running = True
                return subprocess.CompletedProcess(command, 0x800710E0)
            if not self.allow_run:
                return subprocess.CompletedProcess(command, 2)
            self.running = True
            return subprocess.CompletedProcess(command, 0)
        if op == "/End":
            self.running = False
            return subprocess.CompletedProcess(command, 0)
        if op == "/Delete":
            self.present = False
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 2)


def _sut(monkeypatch, fake: FakeScheduler) -> tasks.Scheduler:
    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "_schtasks_executable", lambda: Path("schtasks.exe"))
    monkeypatch.setattr(tasks, "_platform", lambda: None)
    monkeypatch.setattr(readiness, "_current_identity", lambda _manifest: None)
    monkeypatch.setattr(
        readiness,
        "status_readiness",
        lambda _manifest, *, task_running: "verified" if task_running else "offline",
    )
    return tasks.Scheduler(runner=fake)


def _task_calls(fake: FakeScheduler) -> list[list[str]]:
    return [row for row in fake.calls if row[0] != "powershell.exe"]


def test_status_reports_os_registered_disabled_not_fake_process_pid(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    data = _sut(monkeypatch, fake).status(manifest)
    assert data["registered"] is True
    assert data["enabled"] is False
    assert data["running"] is False
    assert data["state"] == "Disabled"
    assert data["readiness"] == "offline"


def test_start_only_exact_new_core_and_requires_real_readiness(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    value = _sut(monkeypatch, fake).start(manifest)
    assert value["running"] is True
    assert value["readiness"] == "verified"
    operations = _task_calls(fake)
    assert any(cmd[1:] == ["/Change", "/TN", "ServerFS v25 Core", "/ENABLE"] for cmd in operations)
    assert any(cmd[1:] == ["/Run", "/TN", "ServerFS v25 Core"] for cmd in operations)
    assert all("ServerFS Native Tunnel" not in " ".join(cmd) for cmd in fake.calls)
    assert all("ServerFS MCP Core" not in " ".join(cmd) for cmd in fake.calls)


def test_windows_export_omits_default_true_after_enable(manifest, monkeypatch) -> None:
    """Windows XSD defaults the omitted Settings/Enabled element to true."""
    fake = FakeScheduler(manifest)
    fake.omit_enabled_when_true = True
    value = _sut(monkeypatch, fake).start(manifest)
    assert value["enabled"] is True
    assert value["running"] is True
    assert value["readiness"] == "verified"


def test_enable_trigger_autostart_does_not_issue_duplicate_run(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.omit_enabled_when_true = True
    fake.auto_start_on_enable = True
    result = _sut(monkeypatch, fake).start(manifest)
    assert result["readiness"] == "verified"
    assert not any(command[1] == "/Run" for command in _task_calls(fake))


def test_run_racing_with_trigger_is_accepted_only_if_running(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.omit_enabled_when_true = True
    fake.run_raced_with_trigger = True
    result = _sut(monkeypatch, fake).start(manifest)
    assert result["running"] is True
    assert result["readiness"] == "verified"


def test_run_refusal_with_omitted_true_rolls_back_same_task(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.omit_enabled_when_true = True
    fake.allow_run = False
    with pytest.raises(tasks.TaskError, match="rollback=disabled-and-ended"):
        _sut(monkeypatch, fake).start(manifest)
    assert fake.enabled is False and fake.running is False
    assert any(
        cmd[1:] == ["/Change", "/TN", "ServerFS v25 Core", "/DISABLE"] for cmd in _task_calls(fake)
    )


def test_invalid_explicit_xml_enabled_is_rejected(manifest) -> None:
    root = ET.fromstring(build_xml(tasks._task_spec(manifest)))
    settings = root.find(f"./{{{NAMESPACE}}}Settings")
    assert settings is not None
    node = settings.find(f"./{{{NAMESPACE}}}Enabled")
    assert node is not None
    node.text = "unknown"
    with pytest.raises(tasks.TaskError, match="enabled state is ambiguous"):
        tasks._enabled(ET.tostring(root, encoding="unicode"))
    node.text = "false"
    assert tasks._enabled(ET.tostring(root, encoding="unicode")) is False
    node.text = "1"
    assert tasks._enabled(ET.tostring(root, encoding="unicode")) is True


def test_existing_running_component_requires_verified_readiness(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.running = True
    service = _sut(monkeypatch, fake)
    healthy = service.start(manifest)
    assert healthy["readiness"] == "verified"
    assert not any(cmd[1] in ("/Run", "/Change", "/End") for cmd in _task_calls(fake))

    monkeypatch.setattr(
        readiness,
        "status_readiness",
        lambda _manifest, *, task_running: "not_verified",
    )
    with pytest.raises(tasks.TaskError, match="already running but readiness is unverified"):
        service.start(manifest)
    assert not any(cmd[1] in ("/Run", "/Change", "/End") for cmd in _task_calls(fake))


def test_install_retries_only_a_private_matching_disabled_definition(
    manifest, monkeypatch, tmp_path: Path
) -> None:
    fake = FakeScheduler(manifest)
    fake.present = False
    service = _sut(monkeypatch, fake)
    monkeypatch.setattr(tasks.storage, "home", lambda _component: tmp_path)
    monkeypatch.setattr(tasks.storage, "check_private", lambda *_a, **_kw: None)
    staged = tmp_path / "task.xml"
    staged.write_bytes(build_xml(tasks._task_spec(manifest)))

    response = service.install(manifest)
    assert response["registered"] is True
    assert response["enabled"] is False
    assert sum(command[1] == "/Create" for command in _task_calls(fake)) == 1
    assert staged.is_file()

    fake.present = False
    foreign = ComponentTask(
        name=manifest.task_name(),
        sid=manifest.sid,
        executable=Path(manifest.pythonw),
        arguments=("-c", "unexpected"),
        enabled=False,
    )
    staged.write_bytes(build_xml(foreign))
    previous = len(_task_calls(fake))
    with pytest.raises(tasks.TaskError, match="does not match this owner"):
        service.install(manifest)
    assert len(_task_calls(fake)) == previous + 1  # only the read-only /Query
    assert _task_calls(fake)[-1][1] == "/Query"


def test_run_failure_records_stage_then_disables_only_new_task(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.allow_run = False
    service = _sut(monkeypatch, fake)
    with pytest.raises(tasks.TaskError, match="start refused at run; rollback=disabled-and-ended"):
        service.start(manifest)
    assert fake.enabled is False
    assert fake.running is False
    assert all("ServerFS Native Tunnel" not in " ".join(cmd) for cmd in fake.calls)


def test_preenabled_ready_task_run_failure_rolls_back_to_disabled(manifest, monkeypatch) -> None:
    """A failed start must not leave a pre-enabled owner auto-startable."""
    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.allow_run = False
    service = _sut(monkeypatch, fake)

    with pytest.raises(tasks.TaskError, match="start refused at run; rollback=disabled-and-ended"):
        service.start(manifest)
    assert fake.enabled is False
    assert fake.running is False
    assert any(
        command[1:] == ["/Change", "/TN", "ServerFS v25 Core", "/DISABLE"]
        for command in _task_calls(fake)
    )
    assert not any("ServerFS Native Tunnel" in " ".join(cmd) for cmd in fake.calls)


def test_preenabled_ready_task_readiness_failure_rolls_back_to_disabled(
    manifest, monkeypatch
) -> None:
    """Even if /Run succeeds, a failed independent probe must fail closed."""
    from types import SimpleNamespace

    fake = FakeScheduler(manifest)
    fake.enabled = True
    service = _sut(monkeypatch, fake)
    clock = _SyntheticClock()
    monkeypatch.setattr(
        tasks,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        readiness,
        "status_readiness",
        lambda _manifest, *, task_running: "not_verified",
    )

    with pytest.raises(
        tasks.TaskError, match="start refused at ready; rollback=disabled-and-ended"
    ):
        service.start(manifest)
    assert clock.current >= 15
    assert fake.enabled is False
    assert fake.running is False
    assert sum(cmd[1] == "/End" for cmd in _task_calls(fake)) == 1


def test_owned_stop_is_idempotent_and_never_kills_foreign_pid(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.running = True
    service = _sut(monkeypatch, fake)
    first = service.stop(manifest)
    second = service.stop(manifest)
    assert first["enabled"] is False and first["running"] is False
    assert second["enabled"] is False and second["running"] is False
    assert sum(args[1] == "/End" for args in _task_calls(fake)) == 1


def test_foreign_xml_refuses_mutation_before_start(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.foreign_xml = True
    with pytest.raises(tasks.TaskError, match="owner contract"):
        _sut(monkeypatch, fake).start(manifest)
    assert not any(cmd[1] in ("/Change", "/Run", "/End") for cmd in _task_calls(fake))


def test_unknown_os_state_is_not_assumed_offline(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)

    def broken_state(commands: list[str]):
        if commands[0] == "powershell.exe":
            return subprocess.CompletedProcess(commands, 0, b"Undetermined")
        return fake(commands)

    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "_schtasks_executable", lambda: Path("schtasks.exe"))
    monkeypatch.setattr(tasks, "_platform", lambda: None)
    with pytest.raises(tasks.TaskError, match="unknown task state"):
        tasks.Scheduler(runner=broken_state).status(manifest)


def test_uninstall_requires_disabled_offline_and_preserves_config(manifest, monkeypatch) -> None:
    fake = FakeScheduler(manifest)
    fake.enabled = True
    service = _sut(monkeypatch, fake)
    with pytest.raises(tasks.TaskError, match="Disabled/offline"):
        service.uninstall(manifest)
    fake.enabled = False
    assert service.uninstall(manifest)["registered"] is False
    assert not fake.present


@pytest.mark.parametrize(
    ("platform_name", "expected_flags"),
    [("nt", 0x08000000), ("posix", 0)],
)
def test_scheduler_subprocess_never_opens_console_on_windows(
    monkeypatch,
    platform_name: str,
    expected_flags: int,
) -> None:
    """The live Cloudflare owner polls Core Task Scheduler every five seconds.

    A windowless pythonw service must not spawn visible PowerShell/conhost.
    Verify the shared _run path, used by both schtasks.exe and powershell.exe,
    while leaving the actual OS Scheduler untouched.
    """
    from types import SimpleNamespace

    observed: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"Running")

    monkeypatch.setattr(tasks, "os", SimpleNamespace(name=platform_name))
    monkeypatch.setattr(tasks.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(tasks.subprocess, "run", fake_run)

    command = ["powershell.exe", "-NoProfile", "-NonInteractive"]
    result = tasks._run(command)
    assert result.returncode == 0
    assert len(observed) == 1
    argv, options = observed[0]
    assert argv == command
    assert options == {
        "capture_output": True,
        "timeout": 25,
        "check": False,
        "creationflags": expected_flags,
    }


def test_pristine_current_user_task_contract_rejects_cross_component_argv(manifest) -> None:
    original = tasks._task_spec(manifest)
    assert original.name == "ServerFS v25 Core"
    assert original.arguments == manifest.task_argv()
    assert original.executable == Path(manifest.pythonw)
    assert "--mcp.command" not in original.arguments


class _SyntheticClock:
    def __init__(self) -> None:
        self.current = 0.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, duration: float) -> None:
        self.current += duration


def test_stop_waits_for_delayed_owner_exit_without_second_end(
    manifest,
    monkeypatch,
) -> None:
    """Scheduler can be Disabled before the exact runtime has exited."""
    from types import SimpleNamespace

    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.running = True
    service = _sut(monkeypatch, fake)
    clock = _SyntheticClock()
    monkeypatch.setattr(
        tasks,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ),
    )
    identities = [object(), object(), None]
    monkeypatch.setattr(
        readiness,
        "_current_identity",
        lambda _manifest: identities.pop(0),
    )

    result = service.stop(manifest)
    assert result["state"] == "Disabled"
    assert not result["running"]
    assert clock.current > 0
    assert sum(cmd[1] == "/End" for cmd in _task_calls(fake)) == 1
    assert not identities


def test_stop_refuses_timeout_while_original_owner_remains_live(
    manifest,
    monkeypatch,
) -> None:
    """No direct process kill or false success after bounded waiting."""
    from types import SimpleNamespace

    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.running = True
    service = _sut(monkeypatch, fake)
    clock = _SyntheticClock()
    monkeypatch.setattr(
        tasks,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ),
    )
    monkeypatch.setattr(
        readiness,
        "_current_identity",
        lambda _manifest: object(),
    )

    with pytest.raises(tasks.TaskError, match="stop is unverified"):
        service.stop(manifest)
    assert clock.current >= 12
    assert fake.enabled is False and fake.running is False
    assert sum(cmd[1] == "/End" for cmd in _task_calls(fake)) == 1


def test_stop_refuses_reenabled_task_during_delayed_kernel_exit(
    manifest,
    monkeypatch,
) -> None:
    """A concurrent task transition cannot be hidden by eventual PID exit."""
    from types import SimpleNamespace

    fake = FakeScheduler(manifest)
    fake.enabled = True
    fake.running = True
    service = _sut(monkeypatch, fake)
    clock = _SyntheticClock()
    monkeypatch.setattr(
        tasks,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ),
    )

    def concurrent_change(_manifest):
        fake.enabled = True
        return object()

    monkeypatch.setattr(readiness, "_current_identity", concurrent_change)
    with pytest.raises(tasks.TaskError, match="registration changed"):
        service.stop(manifest)
    assert clock.current > 0
    assert sum(cmd[1] == "/End" for cmd in _task_calls(fake)) == 1
