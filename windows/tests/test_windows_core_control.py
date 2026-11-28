"""W1e Core task control: mocks only; never registers or ends an OS task."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_core_control as control
from serverfs_mcp import windows_core_service as runtime
from serverfs_mcp import windows_core_task_spec as contract

SID = "S-1-5-21-100-200-300-1000"


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "service-manager" / "mcp-core"
    home.mkdir(parents=True)
    config = tmp_path / "private" / "serverfs.toml"
    config.parent.mkdir()
    config.write_text("[workdirs]\n", encoding="utf-8")
    auth = tmp_path / "private" / "auth.env"
    auth.write_text("synthetic-only", encoding="utf-8")
    pythonw = tmp_path / "core-owner-env" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"synthetic-binary")
    spec = contract.CoreTaskSpec(
        sid=SID,
        pythonw=pythonw,
        config_path=config,
        auth_file=auth,
        port=19876,
        hostname="core.example.test",
    )
    monkeypatch.setattr(runtime, "_ensure_windows", lambda: None)
    monkeypatch.setattr(control, "_current_sid", lambda: SID)
    monkeypatch.setattr(control, "service_home", lambda: home)
    monkeypatch.setattr(control, "_private_home", lambda **kwargs: home)
    monkeypatch.setattr(control, "_dedicated_pythonw", lambda: pythonw)
    monkeypatch.setattr(control, "_require_separate_root", lambda *_: None)
    monkeypatch.setattr(control, "_check_private", lambda path, **kwargs: None)
    from serverfs_mcp import windows_private_artifacts

    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda path, sid: None)
    monkeypatch.setattr(runtime, "validate_runtime", lambda spec, **kwargs: (config, auth, {}))
    return spec


def _task_xml(spec: contract.CoreTaskSpec, *, enabled: bool = False) -> str:
    from dataclasses import replace

    return contract.build_task_xml(
        replace(spec, enabled=enabled), boundary="2026-11-11T08:00:00+08:00"
    ).decode("utf-16")


def _mock_registration(monkeypatch, spec, *, create_exit: int = 0, enabled: bool = False):
    commands: list[tuple[str, ...]] = []

    def schtasks(*args: str):
        commands.append(args)
        return subprocess.CompletedProcess(args, create_exit if args[0] == "/Create" else 0)

    def query(*, optional: bool = False):
        if not any(args[0] == "/Create" for args in commands):
            return None
        if commands[-1][0] == "/Delete":
            return None
        return _task_xml(spec, enabled=enabled)

    monkeypatch.setattr(control, "_task_call", schtasks)
    monkeypatch.setattr(control, "_registered_xml", query)
    return commands


def test_core_mutex_is_held_until_http_server_exits(site, monkeypatch) -> None:
    import ctypes

    created: list[str] = []
    closed: list[int] = []

    class CallableAPI:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def create(_attributes, _initial, name: str) -> int:
        created.append(name)
        return 4242

    kernel = SimpleNamespace(
        CreateMutexW=CallableAPI(create),
        CloseHandle=CallableAPI(lambda handle: closed.append(handle)),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: None, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    with control.core_owner_mutex():
        assert not closed
    assert closed == [4242]
    assert len(created) == 1
    assert created[0].startswith("Local\\serverfs-mcp-core-v1-")


def test_duplicate_core_mutex_refuses_and_closes_handle(site, monkeypatch) -> None:
    import ctypes

    closed: list[int] = []

    class CallableAPI:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    kernel = SimpleNamespace(
        CreateMutexW=CallableAPI(lambda *args: 4343),
        CloseHandle=CallableAPI(lambda handle: closed.append(handle)),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: None, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183, raising=False)
    with pytest.raises(control.CoreControlError, match="another Core"):
        with control.core_owner_mutex():
            pytest.fail("duplicate process must not enter the Core owner region")
    assert closed == [4343]


def test_component_paths_are_separate_from_tunnel_bridge(tmp_path, monkeypatch) -> None:
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    assert control.service_home() == (
        tmp_path / "ServerFS" / "service-manager" / "windows" / "mcp-core"
    )
    assert control.manifest_path().parent == control.service_home()
    assert contract.TASK_NAME == "ServerFS MCP Core"
    assert contract.TASK_NAME not in ("ServerFS Agent Bridge", "ServerFS Native Tunnel")


def test_fresh_install_stages_disabled_exact_task_only(site, monkeypatch) -> None:
    commands = _mock_registration(monkeypatch, site)
    snapshot = control.install_disabled(site)
    assert snapshot["task_name"] == contract.TASK_NAME
    assert snapshot["registered"] is True
    assert snapshot["task_valid"] is True
    assert snapshot["enabled"] is False
    assert len(commands) >= 1
    assert commands[0][:3] == ("/Create", "/TN", contract.TASK_NAME)
    assert "/F" not in commands[0]
    assert not any(args[0] in ("/Run", "/End", "/Change") for args in commands)
    manifest = json.loads(control.manifest_path().read_text(encoding="utf-8"))
    assert contract.validate_authored_xml(control.task_xml_path().read_bytes(), manifest) == (
        True,
        "",
    )
    assert control._state_is_pristine()


def test_enabled_task_definition_refuses_before_registration(site, monkeypatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(control, "_task_call", lambda *_: pytest.fail("must not touch Scheduler"))
    with pytest.raises(control.CoreControlError, match="disabled task definition"):
        control.install_disabled(replace(site, enabled=True))
    assert not control.manifest_path().exists()


def test_existing_registration_refuses_without_staging(site, monkeypatch) -> None:
    monkeypatch.setattr(control, "_registered_xml", lambda **kw: _task_xml(site))
    monkeypatch.setattr(control, "_task_call", lambda *_: pytest.fail("must not mutate Scheduler"))
    with pytest.raises(control.CoreControlError, match="already registered"):
        control.install_disabled(site)
    assert not control.manifest_path().exists()


def test_private_directory_acl_failure_refuses_before_mutation(site, monkeypatch) -> None:
    def refused(**kwargs):
        raise control.CoreControlError("DACL failed")

    monkeypatch.setattr(control, "_private_home", refused)
    monkeypatch.setattr(control, "_task_call", lambda *_: pytest.fail("must not touch Scheduler"))
    with pytest.raises(control.CoreControlError, match="DACL failed"):
        control.install_disabled(site)


def test_ambiguous_create_failure_preserves_all_evidence(site, monkeypatch) -> None:
    commands = _mock_registration(monkeypatch, site, create_exit=1)
    with pytest.raises(control.CoreControlError, match="rollback incomplete"):
        control.install_disabled(site)
    assert [args[0] for args in commands] == ["/Create"]
    assert control.task_xml_path().exists()
    assert control.manifest_path().exists()
    assert control.state_path().exists()


def test_unexpected_enabled_registration_is_not_deleted(site, monkeypatch) -> None:
    commands = _mock_registration(monkeypatch, site, enabled=True)
    with pytest.raises(control.CoreControlError, match="rollback incomplete"):
        control.install_disabled(site)
    assert [args[0] for args in commands] == ["/Create"]
    assert control.manifest_path().exists()
    assert control.state_path().exists()


def test_status_failure_after_safe_create_rolls_back_exact_disabled_task(site, monkeypatch) -> None:
    commands = _mock_registration(monkeypatch, site)
    monkeypatch.setattr(
        control,
        "status",
        lambda: (_ for _ in ()).throw(control.CoreControlError("status unavailable")),
    )
    with pytest.raises(control.CoreControlError, match="status unavailable"):
        control.install_disabled(site)
    assert [args[0] for args in commands] == ["/Create", "/Delete"]
    assert not control.manifest_path().exists()
    assert not control.task_xml_path().exists()
    assert not control.state_path().exists()


def test_read_manifest_requires_exact_owner_and_registered_contract(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site))
    manifest, _xml = control._require_owned()
    assert manifest["sid"] == SID
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site, enabled=True))
    # Enabled is an operational state, not task identity drift.
    assert control._require_owned()[0]["sid"] == SID
    monkeypatch.setattr(control, "_current_sid", lambda: "S-1-5-21-foreign")
    with pytest.raises(control.CoreControlError, match="another user"):
        control._require_owned()


def test_previous_generation_must_be_proven_stopped(site, monkeypatch) -> None:
    from serverfs_mcp import windows_process

    old = {
        "schema_version": 1,
        "component": "mcp-core",
        "stage": "owner-started",
        "core_pid": 12345,
        "core_image_path": str(site.pythonw),
        "core_creation_time": 123456,
    }
    monkeypatch.setattr(control, "_read_state", lambda: old)
    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(pid=pid, image_path=site.pythonw, creation_time=123456),
    )
    with pytest.raises(control.CoreControlError, match="still alive"):
        control._require_previous_generation_stopped()


def test_reused_pid_does_not_block_previous_generation_recovery(site, monkeypatch) -> None:
    from serverfs_mcp import windows_process

    previous = {
        "schema_version": 1,
        "component": "mcp-core",
        "stage": "owner-started",
        "core_pid": 12345,
        "core_image_path": str(site.pythonw),
        "core_creation_time": 123456,
    }
    monkeypatch.setattr(control, "_read_state", lambda: previous)
    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(pid=pid, image_path=site.pythonw, creation_time=999999),
    )
    control._require_previous_generation_stopped()


def test_unreadable_but_present_pid_refuses_recovery(site, monkeypatch) -> None:
    from serverfs_mcp import windows_process

    previous = {
        "schema_version": 1,
        "component": "mcp-core",
        "stage": "owner-started",
        "core_pid": 12345,
        "core_image_path": str(site.pythonw),
        "core_creation_time": 123456,
    }
    monkeypatch.setattr(control, "_read_state", lambda: previous)
    monkeypatch.setattr(windows_process, "process_identity", lambda pid: None)
    monkeypatch.setattr(
        windows_process,
        "process_entries",
        lambda: (SimpleNamespace(pid=12345),),
    )
    with pytest.raises(control.CoreControlError, match="cannot be read"):
        control._require_previous_generation_stopped()


def test_confirmed_absent_previous_pid_allows_recovery(site, monkeypatch) -> None:
    from serverfs_mcp import windows_process

    previous = {
        "schema_version": 1,
        "component": "mcp-core",
        "stage": "owner-started",
        "core_pid": 12345,
        "core_image_path": str(site.pythonw),
        "core_creation_time": 123456,
    }
    monkeypatch.setattr(control, "_read_state", lambda: previous)
    monkeypatch.setattr(windows_process, "process_identity", lambda pid: None)
    monkeypatch.setattr(windows_process, "process_entries", lambda: ())
    control._require_previous_generation_stopped()


def test_runtime_owner_refuses_disabled_task(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site))
    runtime_spec = runtime.CoreRuntimeSpec(
        site.config_path, site.auth_file, site.port, site.hostname, SID
    )
    with pytest.raises(control.CoreControlError, match="disabled"):
        control.record_runtime_owner(runtime_spec)
    assert control._state_is_pristine()


def test_runtime_owner_refuses_wrong_python_image(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site, enabled=True))
    monkeypatch.setattr(control.sys, "executable", str(site.pythonw))
    monkeypatch.setattr(control.sys, "prefix", str(site.pythonw.parent.parent))
    from serverfs_mcp import windows_process

    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(
            pid=pid, image_path=site.pythonw.with_name("other.exe"), creation_time=12345
        ),
    )
    with pytest.raises(control.CoreControlError, match="image"):
        control.record_runtime_owner(
            runtime.CoreRuntimeSpec(site.config_path, site.auth_file, site.port, site.hostname, SID)
        )
    assert control._state_is_pristine()


def test_runtime_owner_records_generation_before_http_serve(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site, enabled=True))
    monkeypatch.setattr(control.sys, "executable", str(site.pythonw))
    monkeypatch.setattr(control.sys, "prefix", str(site.pythonw.parent.parent))
    from serverfs_mcp import windows_process

    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(pid=pid, image_path=site.pythonw, creation_time=12345),
    )
    control.record_runtime_owner(
        runtime.CoreRuntimeSpec(site.config_path, site.auth_file, site.port, site.hostname, SID)
    )
    state = json.loads(control.state_path().read_text(encoding="utf-8"))
    assert state["stage"] == "owner-started"
    assert state["core_image_path"] == str(site.pythonw)
    assert state["core_creation_time"] == 12345
    assert not control._state_is_pristine()


def test_runtime_owner_accepts_attested_base_pythonw_image(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kw: _task_xml(site, enabled=True))
    monkeypatch.setattr(control.sys, "prefix", str(site.pythonw.parent.parent))
    monkeypatch.setattr(control.sys, "executable", str(site.pythonw))
    base = site.pythonw.parent.parent.parent / "base"
    base.mkdir()
    basew = base / "pythonw.exe"
    basew.write_bytes(b"trusted-native-pythonw")
    monkeypatch.setattr(control.sys, "_base_executable", str(basew), raising=False)
    from serverfs_mcp import windows_process

    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: SimpleNamespace(pid=pid, image_path=basew, creation_time=23456),
    )
    control.record_runtime_owner(
        runtime.CoreRuntimeSpec(site.config_path, site.auth_file, site.port, site.hostname, SID)
    )
    data = json.loads(control.state_path().read_text(encoding="utf-8"))
    assert data["stage"] == "owner-started"
    assert data["core_image_path"] == str(basew)
    assert data["core_creation_time"] == 23456


def test_status_never_equates_pid_alone_with_http_readiness(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site, enabled=True))
    from serverfs_mcp import windows_process

    monkeypatch.setattr(windows_process, "same_process", lambda identity: False)
    result = control.status()
    assert result["core_alive"] is False
    assert result["core_pid"] is None
    assert result["http_readiness"] == "not_verified"
    assert result["listener_owner"] == "not_verified"


def test_pause_only_disables_triggers_and_not_the_core_process(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site, enabled=False))
    monkeypatch.setattr(control, "_expect_task_success", lambda *args: calls.append(args))
    assert control.pause()["task_name"] == contract.TASK_NAME
    assert calls == [("/Change", "/TN", contract.TASK_NAME, "/DISABLE")]


def test_start_restart_refuse_without_owned_registration(monkeypatch) -> None:
    monkeypatch.setattr(
        control,
        "_require_owned",
        lambda: (_ for _ in ()).throw(control.CoreControlError("owner unavailable")),
    )
    with pytest.raises(control.CoreControlError, match="owner unavailable"):
        control.start()
    with pytest.raises(control.CoreControlError, match="owner unavailable"):
        control.restart()


def test_stop_and_uninstall_require_never_started_disabled_task(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    monkeypatch.setattr(control, "_registered_xml", lambda **kwargs: _task_xml(site))
    assert control.stop()["enabled"] is False
    state = json.loads(control.state_path().read_text(encoding="utf-8"))
    state["stage"] = "owner-started"
    control.state_path().write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(control.CoreControlError, match="unsafe"):
        control.uninstall_offline()


def test_offline_removal_keeps_unrelated_logs_and_workdirs(site, monkeypatch) -> None:
    _mock_registration(monkeypatch, site)
    control.install_disabled(site)
    log = control.service_home() / "logs" / "lifecycle.log"
    log.parent.mkdir()
    log.write_text("preserve", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def schtasks(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(control, "_task_call", schtasks)
    monkeypatch.setattr(
        control,
        "_registered_xml",
        lambda **kwargs: None if calls and calls[-1][0] == "/Delete" else _task_xml(site),
    )
    control.uninstall_offline()
    assert calls == [("/Delete", "/TN", contract.TASK_NAME, "/F")]
    assert log.read_text(encoding="utf-8") == "preserve"
    assert site.config_path.is_file()
    assert site.auth_file.is_file()
    assert not control.manifest_path().exists()
    assert not control.state_path().exists()


def test_controller_has_no_active_task_kill_or_global_service_stop() -> None:
    source = Path(control.__file__).read_text(encoding="utf-8")
    for forbidden in ('"taskkill"', "run_with_agent(", '"ServerFS Native Tunnel"'):
        assert forbidden not in source
    assert '"/End", "/TN", task_contract.TASK_NAME' in source
