"""W1b-3 source-only conservative control tests.

All Scheduler operations and Windows ownership are mocked. These tests MUST
NOT start the real same-SID Bridge, interact with production provider guards,
or modify any Task Scheduler registration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_bridge_control as control
from serverfs_mcp import windows_bridge_service as runtime
from serverfs_mcp import windows_bridge_task_spec as contract

SID = "S-1-5-21-100-200-300-1000"


def _temp_task_xml(path: Path, *, enabled: bool) -> str:
    spec = contract.BridgeTaskSpec(
        user_sid=SID,
        task_executable=path / "pythonw.exe",
        config_path=path / "serverfs.toml",
        enabled=enabled,
    )
    return contract.build_task_xml(spec, start_boundary="2026-11-10T22:00:00+08:00").decode(
        "utf-16"
    )


def _isolated_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Use a temporary service home; disable every Windows or OS task seam."""
    home = tmp_path / "bridge-state"
    # The real W2b installer deliberately requires a pre-provisioned private
    # directory. Create only this synthetic fixture's directory up front.
    home.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text("[server]\n", encoding="utf-8")
    monkeypatch.setattr(control, "_ensure_windows", lambda: None)
    monkeypatch.setattr(control, "_exclude_exposed_workdirs", lambda *_: None)
    monkeypatch.setattr(control, "_ensure_backend", lambda _spec: None)
    monkeypatch.setattr(control, "_current_sid", lambda: SID)
    from serverfs_mcp import windows_core_control, windows_private_artifacts

    monkeypatch.setattr(windows_core_control, "_check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: SID)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args: None)
    monkeypatch.setattr(control, "_dedicated_pythonw", lambda: tmp_path / "pythonw.exe")

    def fake_home(*, create: bool = False) -> Path:
        if create:
            home.mkdir(parents=True, exist_ok=True)
        return home

    monkeypatch.setattr(control, "_check_private_home", fake_home)
    monkeypatch.setattr(runtime, "service_home", lambda: home)
    monkeypatch.setattr(
        runtime,
        "status",
        lambda: {
            "component": "agent-bridge",
            "registered": True,
            "task_valid": True,
            "enabled": False,
        },
    )
    return config


def test_fixed_task_identity_never_matches_a_legacy_owner() -> None:
    assert contract.TASK_NAME == "ServerFS Agent Bridge"
    assert contract.TASK_NAME != "ServerFS Native Tunnel"
    assert contract.TASK_NAME != "ServerFS Cloudflare WorkPC"


def test_fresh_install_is_disabled_without_force_and_is_component_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []

    def schtasks(*args: str) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    def query(*, optional: bool = False) -> str | None:
        return _temp_task_xml(tmp_path, enabled=False) if commands else None

    monkeypatch.setattr(control, "_schtasks", schtasks)
    monkeypatch.setattr(control, "_registered_xml", query)
    # Real authored bytes must be generated and validated against the manifest.
    result = control.install_disabled(config)
    assert result["component"] == "agent-bridge"
    assert len(commands) == 1
    assert commands[0][:3] == ("/Create", "/TN", contract.TASK_NAME)
    assert "/F" not in commands[0]
    assert "/Run" not in commands[0]
    assert "/ENABLE" not in commands[0]

    manifest = json.loads(runtime.manifest_path().read_text(encoding="utf-8"))
    xml = runtime.authored_task_path().read_bytes()
    assert contract.validate_authored_task_xml(xml, manifest) == (True, "")
    assert manifest["component"] == "agent-bridge"
    staged = json.loads(runtime.watchdog_state_path().read_text(encoding="utf-8"))
    assert staged["stage"] == "disabled-never-started"
    assert staged["runner_restart_count"] == 0


def test_existing_task_refuses_install_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    monkeypatch.setattr(control, "_registered_xml", lambda **_: "<Task/>")
    monkeypatch.setattr(
        control, "_schtasks", lambda *_: pytest.fail("should not run a Task Scheduler command")
    )
    with pytest.raises(control.BridgeControlError, match="already exists"):
        control.install_disabled(config)
    assert not runtime.manifest_path().exists()


def test_registration_failure_does_not_delete_an_unknown_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(control, "_registered_xml", lambda **_: None)

    def rejected(*args: str) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 1, stdout=b"")

    monkeypatch.setattr(control, "_schtasks", rejected)
    with pytest.raises(control.BridgeControlError, match="operation failed"):
        control.install_disabled(config)
    assert len(commands) == 1
    assert commands[0][0] == "/Create"
    assert not runtime.manifest_path().exists()
    assert not runtime.authored_task_path().exists()
    assert not runtime.watchdog_state_path().exists()


def test_failed_create_that_registered_unknown_task_preserves_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    calls: list[tuple[str, ...]] = []

    def query(*, optional: bool = False) -> str | None:
        return None if not calls else "<Task/>"

    def schtasks(*args: str) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout=b"")

    monkeypatch.setattr(control, "_registered_xml", query)
    monkeypatch.setattr(control, "_schtasks", schtasks)
    with pytest.raises(control.BridgeControlError, match="rollback incomplete"):
        control.install_disabled(config)
    assert len(calls) == 1 and calls[0][0] == "/Create"
    assert runtime.authored_task_path().exists()
    assert runtime.manifest_path().exists()
    assert runtime.watchdog_state_path().exists()


def test_unexpectedly_enabled_owner_is_preserved_not_force_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []

    def query(*, optional: bool = False) -> str | None:
        if not commands:
            return None
        return _temp_task_xml(tmp_path, enabled=True)

    monkeypatch.setattr(control, "_registered_xml", query)

    def fake_task(*args: str) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    monkeypatch.setattr(control, "_schtasks", fake_task)
    with pytest.raises(control.BridgeControlError, match="rollback incomplete"):
        control.install_disabled(config)
    assert [part[0] for part in commands] == ["/Create"]
    assert runtime.manifest_path().is_file()
    assert runtime.authored_task_path().is_file()
    assert runtime.watchdog_state_path().is_file()


def test_safe_failure_after_disabled_registration_rolls_back_exact_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _isolated_stage(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []

    def query(*, optional: bool = False) -> str | None:
        if not commands or commands[-1][0] == "/Delete":
            return None
        return _temp_task_xml(tmp_path, enabled=False)

    def fake_task(*args: str) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    def fail_status() -> dict:
        raise control.BridgeControlError("status was unavailable")

    monkeypatch.setattr(control, "_registered_xml", query)
    monkeypatch.setattr(control, "_schtasks", fake_task)
    monkeypatch.setattr(runtime, "status", fail_status)
    with pytest.raises(control.BridgeControlError, match="status was unavailable"):
        control.install_disabled(config)
    assert [part[0] for part in commands] == ["/Create", "/Delete"]
    assert all(part[2] == contract.TASK_NAME for part in commands)
    assert not runtime.manifest_path().exists()
    assert not runtime.authored_task_path().exists()
    assert not runtime.watchdog_state_path().exists()


def test_file_stage_is_exclusive_and_does_not_overwrite_an_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_core_control, windows_private_artifacts

    monkeypatch.setattr(control, "_current_sid", lambda: SID)
    monkeypatch.setattr(windows_core_control, "_check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: SID)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args: None)
    path = tmp_path / "existing.xml"
    path.write_bytes(b"original")
    with pytest.raises(control.BridgeControlError, match="could not be written"):
        control._atomic_write(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".existing.xml.tmp-*"))


def test_exposed_workdir_overlap_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_config

    config = tmp_path / "serverfs.toml"
    config.write_text("not parsed", encoding="utf-8")
    home = tmp_path / "workspace" / "service-state"
    workdir = SimpleNamespace(root=tmp_path / "workspace")
    settings = SimpleNamespace(bridge_required=True)
    monkeypatch.setattr(native_config, "load_native_config", lambda _path: ([workdir], settings))
    with pytest.raises(control.BridgeControlError, match="overlaps"):
        control._exclude_exposed_workdirs(config, home)


def test_agent_disabled_config_refuses_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_config

    config = tmp_path / "serverfs.toml"
    config.write_text("not parsed", encoding="utf-8")
    monkeypatch.setattr(
        native_config, "load_native_config", lambda _: ([], SimpleNamespace(bridge_required=False))
    )
    with pytest.raises(control.BridgeControlError, match="does not enable"):
        control._exclude_exposed_workdirs(config, tmp_path / "private")


def test_pause_disables_future_starts_but_never_ends_a_running_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        control, "_require_owned", lambda: ({}, _temp_task_xml(tmp_path, enabled=True))
    )
    monkeypatch.setattr(control, "_expect_success", lambda *args: commands.append(args))
    monkeypatch.setattr(
        control, "_registered_xml", lambda **_: _temp_task_xml(tmp_path, enabled=False)
    )
    monkeypatch.setattr(control, "status", lambda: {"runner_alive": True, "enabled": False})
    assert control.pause()["runner_alive"] is True
    assert commands == [("/Change", "/TN", contract.TASK_NAME, "/DISABLE")]


def test_never_started_state_is_only_offline_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "service_home", lambda: tmp_path)
    pristine = {
        "component": "agent-bridge",
        "stage": "disabled-never-started",
        "runner_restart_count": 0,
        "watchdog_pid": None,
        "managed_runner_pid": None,
    }
    monkeypatch.setattr(runtime, "_read_state", lambda path: dict(pristine))
    monkeypatch.setattr(
        runtime,
        "_identity_alive",
        lambda state, role: False,
    )
    control._assert_idle()
    altered = dict(pristine, stage="owner-started", runner_restart_count=1)
    monkeypatch.setattr(runtime, "_read_state", lambda path: altered)
    with pytest.raises(control.BridgeControlError, match="has started"):
        control._assert_idle()
    monkeypatch.setattr(runtime, "_read_state", lambda path: {})
    with pytest.raises(control.BridgeControlError, match="unverified"):
        control._assert_idle()


def test_stop_refuses_active_owner_without_task_scheduler_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        control, "_require_owned", lambda: ({}, _temp_task_xml(tmp_path, enabled=False))
    )
    monkeypatch.setattr(
        control,
        "_assert_idle",
        lambda: (_ for _ in ()).throw(control.BridgeControlError("still active")),
    )
    monkeypatch.setattr(
        control, "_schtasks", lambda *_: pytest.fail("active provider must never be killed")
    )
    with pytest.raises(control.BridgeControlError, match="still active"):
        control.stop()


def test_stop_only_reports_already_idle_disabled_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        control, "_require_owned", lambda: ({}, _temp_task_xml(tmp_path, enabled=False))
    )
    monkeypatch.setattr(control, "_assert_idle", lambda: None)
    monkeypatch.setattr(control, "status", lambda: {"enabled": False, "runner_alive": False})
    assert control.stop() == {"enabled": False, "runner_alive": False}


def test_start_and_restart_are_closed_until_provider_safe_handover() -> None:
    with pytest.raises(control.BridgeControlError, match="requires W2"):
        control.start()
    with pytest.raises(control.BridgeControlError, match="requires W2"):
        control.restart()


def test_uninstall_only_removes_verified_disabled_offline_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_stage(monkeypatch, tmp_path)
    home = runtime.service_home()
    home.mkdir(exist_ok=True)
    runtime.authored_task_path().write_bytes(b"owned-task")
    runtime.manifest_path().write_text("{}", encoding="utf-8")
    runtime.watchdog_state_path().write_text(
        '{"schema_version":1,"component":"agent-bridge","stage":"disabled-never-started"}',
        encoding="utf-8",
    )
    (home / "logs").mkdir()
    (home / "logs" / "lifecycle.log").write_text("preserved", encoding="utf-8")

    monkeypatch.setattr(
        control, "_require_owned", lambda: ({}, _temp_task_xml(tmp_path, enabled=False))
    )
    monkeypatch.setattr(control, "_assert_idle", lambda: None)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(control, "_expect_success", lambda *args: commands.append(args))
    monkeypatch.setattr(control, "_registered_xml", lambda **_: None)
    control.uninstall_offline()
    assert commands == [("/Delete", "/TN", contract.TASK_NAME, "/F")]
    assert not runtime.manifest_path().exists()
    assert not runtime.authored_task_path().exists()
    assert not runtime.watchdog_state_path().exists()
    assert (home / "logs" / "lifecycle.log").read_text(encoding="utf-8") == "preserved"


def test_uninstall_refuses_a_still_enabled_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        control, "_require_owned", lambda: ({}, _temp_task_xml(tmp_path, enabled=True))
    )
    monkeypatch.setattr(
        control, "_expect_success", lambda *args: pytest.fail("unexpected Scheduler deletion")
    )
    with pytest.raises(control.BridgeControlError, match="disable"):
        control.uninstall_offline()


def test_no_direct_stopping_or_legacy_task_invocation_in_controller() -> None:
    import inspect

    source = inspect.getsource(control)
    assert '"/End"' not in source
    assert '"/Run"' not in source
    assert '"taskkill"' not in source
    assert '"/ENABLE"' not in source
    assert '"/Create"' in source
    assert '"/Delete"' in source
    assert "subprocess.Popen(" not in source
