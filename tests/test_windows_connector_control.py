"""W1f disabled-only connector lifecycle source tests; no host Task Scheduler writes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from serverfs_mcp import windows_connector_control as control
from serverfs_mcp import windows_connector_service as service
from serverfs_mcp import windows_connector_task_spec as contract

SID = "S-1-5-21-100-200-300-1000"


@pytest.fixture(params=("openai", "cloudflare"))
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    from serverfs_mcp import native_lifecycle

    kind = request.param
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path)
    monkeypatch.setattr(service, "_check_windows", lambda: None)
    monkeypatch.setattr(service, "_check_private", lambda path, **kwargs: None)
    from serverfs_mcp import windows_private_artifacts

    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda path, sid: None)
    monkeypatch.setattr(control, "_sid", lambda: SID)
    from serverfs_mcp import windows_core_control

    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: SID)
    monkeypatch.setattr(windows_core_control, "_check_private", lambda path, **kwargs: None)
    monkeypatch.setattr(control, "_check_home", lambda which: None)
    exe = tmp_path / f"{kind}-env" / "Scripts" / "pythonw.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake")
    monkeypatch.setattr(control, "_pythonw", lambda which: exe)
    home = service.service_home(kind)
    home.mkdir(parents=True)
    (home / "logs").mkdir()
    for name in ("connector.stdout.log", "connector.stderr.log"):
        (home / "logs" / name).write_bytes(b"")
    service.policy_path(kind).write_bytes(b'{"schema_version":1}')
    monkeypatch.setattr(service, "load_policy", lambda which: {"kind": kind, "core_port": 19876})
    monkeypatch.setattr(service, "validate_runtime_inputs", lambda *args: None)
    return kind, exe


def registered_xml(kind: str, executable: Path, *, enabled: bool = False) -> str:
    task = contract.ConnectorTaskSpec(
        kind=kind,
        sid=SID,
        pythonw=executable,
        policy_file=service.policy_path(kind),
        enabled=enabled,
    )
    return contract.build_task_xml(task, boundary="2026-11-11T08:00:00+08:00").decode("utf-16")


def mocked_scheduler(monkeypatch, kind, exe, *, enabled: bool = False, create_exit: int = 0):
    commands = []

    def invoke(*args: str):
        commands.append(args)
        return subprocess.CompletedProcess(args, create_exit if args[0] == "/Create" else 0)

    def query(_kind, *, optional=False):
        if not any(args[0] == "/Create" for args in commands):
            return None
        if commands[-1][0] == "/Delete":
            return None
        return registered_xml(kind, exe, enabled=enabled)

    monkeypatch.setattr(control, "_call", invoke)
    monkeypatch.setattr(control, "_registered", query)
    return commands


def test_install_stages_only_fixed_disabled_task(fixture, monkeypatch) -> None:
    kind, exe = fixture
    commands = mocked_scheduler(monkeypatch, kind, exe)
    snapshot = control.install_disabled(kind)
    assert snapshot["task_name"] == contract.TASK_NAMES[kind]
    assert snapshot["enabled"] is False
    assert snapshot["task_valid"] is True
    assert commands[0][:3] == ("/Create", "/TN", contract.TASK_NAMES[kind])
    assert all(args[0] not in ("/End", "/Run", "/Change") for args in commands)
    assert "/F" not in commands[0]
    assert control._pristine(kind)


def test_existing_task_name_refuses_before_creating_files(fixture, monkeypatch) -> None:
    kind, exe = fixture
    monkeypatch.setattr(
        control,
        "_registered",
        lambda which, **kwargs: registered_xml(kind, exe),
    )
    monkeypatch.setattr(
        control, "_call", lambda *args: pytest.fail("should not mutate an existing task")
    )
    with pytest.raises(control.ConnectorControlError, match="already exists"):
        control.install_disabled(kind)
    assert not service.manifest_path(kind).exists()


def test_failed_create_that_may_have_registered_task_preserves_files(fixture, monkeypatch) -> None:
    kind, exe = fixture
    commands = mocked_scheduler(monkeypatch, kind, exe, create_exit=1)
    with pytest.raises(control.ConnectorControlError, match="rollback incomplete"):
        control.install_disabled(kind)
    assert [cmd[0] for cmd in commands] == ["/Create"]
    assert service.manifest_path(kind).exists()
    assert service.task_path(kind).exists()
    assert service.state_path(kind).exists()


def test_unexpected_enabled_task_never_deleted_by_rollback(fixture, monkeypatch) -> None:
    kind, exe = fixture
    commands = mocked_scheduler(monkeypatch, kind, exe, enabled=True)
    with pytest.raises(control.ConnectorControlError, match="rollback incomplete"):
        control.install_disabled(kind)
    assert [cmd[0] for cmd in commands] == ["/Create"]
    assert service.manifest_path(kind).exists()


def test_valid_offline_rollback_deletes_only_its_own_task(fixture, monkeypatch) -> None:
    kind, exe = fixture
    commands = mocked_scheduler(monkeypatch, kind, exe)

    def missing_status(which):
        raise control.ConnectorControlError("status unavailable")

    monkeypatch.setattr(control, "status", missing_status)
    with pytest.raises(control.ConnectorControlError, match="status unavailable"):
        control.install_disabled(kind)
    assert [cmd[0] for cmd in commands] == ["/Create", "/Delete"]
    assert all(cmd[2] == contract.TASK_NAMES[kind] for cmd in commands)
    assert not service.manifest_path(kind).exists()
    assert not service.task_path(kind).exists()
    assert not service.state_path(kind).exists()


def test_runtime_refuses_disabled_task_before_stamping_process(fixture, monkeypatch) -> None:
    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    monkeypatch.setattr(control, "_registered", lambda which, **kwargs: registered_xml(kind, exe))
    with pytest.raises(control.ConnectorControlError, match="disabled"):
        control.record_runtime_owner(kind)
    assert control._pristine(kind)


def test_runtime_refuses_wrong_interpreter_and_foreign_registered_identity(
    fixture, monkeypatch
) -> None:
    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    monkeypatch.setattr(
        control,
        "_registered",
        lambda which, **kwargs: registered_xml(kind, exe, enabled=True),
    )
    monkeypatch.setattr(control.sys, "executable", str(exe.with_name("foreign.exe")))
    with pytest.raises(control.ConnectorControlError, match="interpreter"):
        control.record_runtime_owner(kind)


def test_connector_owner_accepts_exact_cp_pythonw_base_redirector(fixture, monkeypatch) -> None:
    from types import SimpleNamespace

    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    monkeypatch.setattr(
        control, "_registered", lambda which, **kwargs: registered_xml(kind, exe, enabled=True)
    )
    monkeypatch.setattr(control.sys, "prefix", str(exe.parent.parent))
    monkeypatch.setattr(control.sys, "executable", str(exe))
    base = exe.parent.parent.parent / ("base-" + kind)
    base.mkdir()
    basew = base / "pythonw.exe"
    basew.write_bytes(b"native-pythonw")
    monkeypatch.setattr(control.sys, "_base_executable", str(basew), raising=False)
    from serverfs_mcp import windows_process

    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(pid=pid, image_path=basew, creation_time=34567),
    )
    control.record_runtime_owner(kind)
    recorded = control._read_state(kind)
    assert recorded["stage"] == "owner-started"
    assert recorded["connector_image_path"] == str(basew)
    assert recorded["connector_creation_time"] == 34567


def test_pause_does_not_end_the_connector_process(fixture, monkeypatch) -> None:
    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    events = []
    monkeypatch.setattr(
        control,
        "_registered",
        lambda which, **kwargs: registered_xml(kind, exe),
    )
    monkeypatch.setattr(control, "_expect", lambda *args: events.append(args))
    assert control.pause(kind)["task_name"] == contract.TASK_NAMES[kind]
    assert events == [("/Change", "/TN", contract.TASK_NAMES[kind], "/DISABLE")]


def test_start_restart_require_the_exact_owned_connector(fixture, monkeypatch) -> None:
    kind, _ = fixture
    monkeypatch.setattr(
        control,
        "_owned",
        lambda *_: (_ for _ in ()).throw(
            control.ConnectorControlError("connector owner unavailable")
        ),
    )
    with pytest.raises(control.ConnectorControlError, match="owner unavailable"):
        control.start(kind)
    with pytest.raises(control.ConnectorControlError, match="owner unavailable"):
        control.restart(kind)


def test_offline_uninstall_never_touches_other_component(fixture, monkeypatch) -> None:
    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    events = []
    monkeypatch.setattr(
        control,
        "_registered",
        lambda which, **kwargs: (
            None if events and events[-1][0] == "/Delete" else registered_xml(kind, exe)
        ),
    )
    monkeypatch.setattr(control, "_expect", lambda *args: events.append(args))
    control.uninstall_offline(kind)
    assert events == [("/Delete", "/TN", contract.TASK_NAMES[kind], "/F")]
    assert not service.manifest_path(kind).exists()
    assert service.policy_path(kind).exists()


def test_unknown_state_never_allows_uninstall(fixture, monkeypatch) -> None:
    kind, exe = fixture
    mocked_scheduler(monkeypatch, kind, exe)
    control.install_disabled(kind)
    state = service.state_path(kind)
    state.write_text('{"schema_version":1,"stage":"owner-started"}', encoding="utf-8")
    monkeypatch.setattr(
        control,
        "_registered",
        lambda which, **kwargs: registered_xml(kind, exe),
    )
    with pytest.raises(control.ConnectorControlError, match="never-started"):
        control.uninstall_offline(kind)
    assert service.manifest_path(kind).exists()


def test_controller_does_not_end_legacy_task_or_launch_provider() -> None:
    source = Path(control.__file__).read_text(encoding="utf-8")
    for forbidden in ('"taskkill"', "subprocess.Popen(", '"ServerFS Native Tunnel"'):
        assert forbidden not in source
    assert '"/End", "/TN", tasks.TASK_NAMES[kind]' in source
