"""W1b-2 pure/isolated watchdog tests; never start the production Bridge or OS task."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import windows_bridge_service as service


def test_runner_identity_and_argv_are_component_scoped(tmp_path: Path) -> None:
    config = tmp_path / "serverfs.toml"
    argv = service._runner_arguments(config)
    assert argv == [
        "-I",
        "-m",
        "serverfs_mcp.windows_bridge_service",
        "_run",
        "--config",
        str(config.resolve()),
    ]
    assert "serverfs_mcp.supervisor" not in str(argv)
    assert "tunnel-client" not in str(argv)
    assert "cloudflared" not in str(argv)


def test_control_plane_tunnel_and_proxy_values_are_scrubbed() -> None:
    polluted = {
        "CONTROL_PLANE_API_KEY": "top-secret",
        "MCP_EXTRA_HEADERS": "Authorization: Bearer secret",
        "TUNNEL_CLIENT_KEY": "client-secret",
        "OPENAI_API_KEY": "client-secret",
        "SERVERFS_PROXY_PASSWORD": "proxy-secret",
        "HTTP_PROXY": "http://secret",
        "no_proxy": "private",
        "PYTHONPATH": "D:/user-controlled-pythonpath",
        "PYTHONHOME": "D:/untrusted-home",
        "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:9999",
        "SERVERFS_BRIDGE_PYTHON": "D:/malicious/python.exe",
        "SERVERFS_DATA_HOME": "D:/Private/ServerFS",
        "PATH": "safe",
    }
    clean = service._runtime_environment(polluted)
    assert set(clean) == {
        "SERVERFS_AGENT_PROXY_URL",
        "SERVERFS_DATA_HOME",
        "PATH",
    }
    assert "secret" not in str(clean)


@pytest.mark.parametrize(
    ("runtime", "previous", "watchdog", "expected"),
    [
        (0, 5.0, False, 10.0),
        (20, 50.0, False, 60.0),
        (300, 60.0, False, 5.0),
        (0, 2.0, True, 4.0),
        (1, 30.0, True, 30.0),
        (300, 30.0, True, 2.0),
    ],
)
def test_restart_policy_is_bounded_and_resets(
    runtime: float, previous: float, watchdog: bool, expected: float
) -> None:
    assert service._next_restart_delay(runtime, previous, watchdog=watchdog) == expected


def test_staged_manifest_is_required_before_ownership(tmp_path: Path, monkeypatch) -> None:
    """No manifest means no task/process: all calls below are mocked pure functions."""
    config = tmp_path / "serverfs.toml"
    config.write_text("[server]\n", encoding="utf-8")
    monkeypatch.setattr(service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(
        service,
        "_read_manifest",
        lambda: (_ for _ in ()).throw(service.BridgeServiceError("no manifest")),
    )
    with pytest.raises(service.BridgeServiceError, match="no manifest"):
        service._validate_staged_artifacts(config)


def test_unregistered_task_refuses_runner_without_starting_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "serverfs.toml"
    monkeypatch.setattr(service, "_validate_staged_artifacts", lambda path: {"ok": True})
    monkeypatch.setattr(
        service,
        "_registered_task_xml",
        lambda: (_ for _ in ()).throw(service.BridgeServiceError("not registered")),
    )
    invoked = []
    monkeypatch.setattr(service, "run_bridge_owner", lambda path: invoked.append(path))
    with pytest.raises(service.BridgeServiceError, match="not registered"):
        service.run_owned_generation(config)
    assert not invoked


def test_disabled_registered_task_refuses_generation(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "serverfs.toml"
    manifest = {"user_sid": "S-1-5-21-100"}
    xml = (
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Settings><Enabled>false</Enabled></Settings></Task>"
    )
    monkeypatch.setattr(service, "_validate_staged_artifacts", lambda path: manifest)
    monkeypatch.setattr(service, "_registered_task_xml", lambda: xml)
    monkeypatch.setattr(
        service.contract, "validate_registered_task", lambda _xml, _manifest: (True, "")
    )
    with pytest.raises(service.BridgeServiceError, match="disabled"):
        service._require_registered_task(config)


def test_run_owned_generation_dispatches_once_after_validation(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "native.toml"
    observed = []
    pinned = tmp_path / "bridge-runtime-env" / "Scripts" / "python.exe"
    monkeypatch.setattr(
        service,
        "_require_registered_task",
        lambda path: {"bridge_executable": str(pinned)},
    )

    def owned(path: Path, *, bridge_python: Path) -> int:
        observed.append((path, bridge_python))
        return 3

    monkeypatch.setattr(service, "run_bridge_owner", owned)
    assert service.run_owned_generation(config) == 3
    assert observed == [(config.resolve(), pinned)]


def test_state_path_is_not_a_legacy_service_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service, "service_home", lambda: tmp_path / "bridge-owner")
    assert service.state_path().parent.name == "bridge-owner"
    assert service.watchdog_state_path().parent.name == "bridge-owner"
    assert service.manifest_path().parent.name == "bridge-owner"
    assert service.authored_task_path().parent.name == "bridge-owner"
    assert "ServerFS Native Tunnel" not in str(service.manifest_path())


def test_atomic_state_is_component_scoped_and_has_schema(monkeypatch, tmp_path: Path) -> None:
    from serverfs_mcp import windows_core_control, windows_private_artifacts

    monkeypatch.setattr(service, "service_home", lambda: tmp_path / "bridge-only")
    service.service_home().mkdir()
    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: "S-1-5-21-100")
    monkeypatch.setattr(windows_core_control, "_check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args: None)
    service._atomic_write_state(service.state_path(), {"generation": 2, "runner_pid": None})
    state = json.loads(service.state_path().read_text(encoding="utf-8"))
    assert state == {"schema_version": 1, "generation": 2, "runner_pid": None}


def test_service_main_only_exposes_internal_modes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(service, "run_owned_generation", lambda _path: 3)
    assert service.main(["_run", "--config", str(tmp_path / "serverfs.toml")]) == 3
    with pytest.raises(SystemExit) as failure:
        service.main(["install", "--config", str(tmp_path / "serverfs.toml")])
    assert failure.value.code == 2


def test_task_spawns_no_core_connector_or_legacy_supervisor() -> None:
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert "serverfs_mcp.supervisor" not in service._runner_arguments(Path.cwd() / "cfg.toml")
    for verb in ('"/Create"', '"/Run"', '"/End"', '"/Delete"', '"CONTROL_PLANE_API_KEY"'):
        assert verb not in source


def test_status_is_read_only_and_never_claims_legacy_pipe_ready(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(service, "_read_manifest", lambda: {"component": "agent-bridge"})
    monkeypatch.setattr(
        service,
        "_registered_task_xml",
        lambda: (
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            "<Settings><Enabled>false</Enabled></Settings></Task>"
        ),
    )
    monkeypatch.setattr(service.contract, "validate_registered_task", lambda *_: (True, ""))
    monkeypatch.setattr(
        service,
        "_read_state",
        lambda path: {
            "watchdog_pid": 77,
            "watchdog_image_path": "C:/Python/pythonw.exe",
            "watchdog_creation_time": 111,
            "managed_runner_pid": 88,
            "managed_runner_image_path": "C:/Python/pythonw.exe",
            "managed_runner_creation_time": 222,
            "runner_restart_count": 1,
        },
    )
    monkeypatch.setattr(
        service,
        "_identity_alive",
        lambda state, prefix: prefix == "watchdog",
    )
    state = service.status()
    assert state["task_name"] == "ServerFS Agent Bridge"
    assert state["task_valid"] is True
    assert state["registered"] is True
    assert state["enabled"] is False
    assert state["watchdog_pid"] == 77
    assert state["runner_pid"] is None
    assert state["bridge_generation_readiness"] == "not_verified"


def test_identity_status_refuses_pid_without_creation_time(monkeypatch) -> None:
    from serverfs_mcp import windows_process

    def forbidden(_identity: object) -> bool:
        raise AssertionError("invalid identity was sent to Windows")

    monkeypatch.setattr(windows_process, "same_process", forbidden)
    assert service._identity_alive({"watchdog_pid": 22}, "watchdog") is False
    assert (
        service._identity_alive(
            {
                "watchdog_pid": 22,
                "watchdog_image_path": "C:/Python/pythonw.exe",
                "watchdog_creation_time": False,
            },
            "watchdog",
        )
        is False
    )


def test_watchdog_supervises_only_its_own_runner_and_closes_job(
    tmp_path: Path, monkeypatch
) -> None:
    """A simulated generation never touches an actual Windows Job or Bridge."""
    from serverfs_mcp import windows_job

    events: list[str] = []

    class FakeJob:
        def open(self) -> None:
            events.append("job.open")

        def assign(self, process: object) -> None:
            events.append("job.assign")

        def resume(self, process: object) -> None:
            events.append("job.resume")

        def close(self) -> None:
            events.append("job.close")

    class FakeProcess:
        pid = 123456

        def wait(self, timeout: float | None = None) -> int:
            events.append("runner.wait")
            return 3

    def fake_popen(command: list[str], **kwargs: object) -> object:
        events.append("runner.spawn")
        assert command[1:5] == ["-I", "-m", "serverfs_mcp.windows_bridge_service", "_run"]
        assert kwargs["stdin"] == service.subprocess.DEVNULL
        assert kwargs["stdout"] == service.subprocess.DEVNULL
        return FakeProcess()

    class StopLoop(Exception):
        pass

    def break_after_one(delay: float) -> None:
        events.append("backoff")
        raise StopLoop

    monkeypatch.setattr(service, "_require_registered_task", lambda path: {})
    monkeypatch.setattr(service, "_process_state", lambda prefix, pid: {"watchdog_pid": 1})
    monkeypatch.setattr(service, "_runner_executable", lambda: tmp_path / "pythonw.exe")
    monkeypatch.setattr(service, "_append_event", lambda name, **fields: events.append(name))
    monkeypatch.setattr(service, "service_home", lambda: tmp_path / "bridge-service")
    service.service_home().mkdir()
    service.log_dir().mkdir()
    (service.log_dir() / "runner.stderr.log").write_bytes(b"")
    monkeypatch.setattr(service, "_private_log_path", lambda name: service.log_dir() / name)
    from serverfs_mcp import windows_core_control, windows_private_artifacts

    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: "S-1-5-21-100")
    monkeypatch.setattr(windows_core_control, "_check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args: None)
    monkeypatch.setattr(windows_job, "WindowsJob", FakeJob)
    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service.time, "sleep", break_after_one)
    with pytest.raises(StopLoop):
        service.run_watchdog(tmp_path / "serverfs.toml")
    assert events[:7] == [
        "job.open",
        "runner.spawn",
        "job.assign",
        "job.resume",
        "bridge_runner_started",
        "runner.wait",
        "job.close",
    ]
    assert events.count("runner.spawn") == 1
    state = json.loads(service.watchdog_state_path().read_text(encoding="utf-8"))
    assert state["last_runner_exit_code"] == 3
    assert state["managed_runner_pid"] is None


def test_failed_assignment_kills_uncontained_runner_before_job_close(
    tmp_path: Path, monkeypatch
) -> None:
    """The suspended launcher must not escape when assignment fails."""
    from serverfs_mcp import windows_job

    events: list[str] = []

    class FakeJob:
        def open(self) -> None:
            events.append("job.open")

        def assign(self, process: object) -> None:
            events.append("job.assign")
            raise OSError("assignment failure")

        def close(self) -> None:
            events.append("job.close")

    class FakeProcess:
        pid = 123456

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            events.append("runner.kill")

        def wait(self, timeout: float | None = None) -> int:
            events.append("runner.wait")
            return 1

    class StopLoop(Exception):
        pass

    monkeypatch.setattr(service, "_require_registered_task", lambda path: {})
    monkeypatch.setattr(service, "_process_state", lambda prefix, pid: {"watchdog_pid": 1})
    monkeypatch.setattr(service, "_runner_executable", lambda: tmp_path / "pythonw.exe")
    monkeypatch.setattr(service, "_append_event", lambda name, **fields: None)
    monkeypatch.setattr(service, "service_home", lambda: tmp_path / "bridge-service")
    service.service_home().mkdir()
    service.log_dir().mkdir()
    (service.log_dir() / "runner.stderr.log").write_bytes(b"")
    monkeypatch.setattr(service, "_private_log_path", lambda name: service.log_dir() / name)
    from serverfs_mcp import windows_core_control, windows_private_artifacts

    monkeypatch.setattr(windows_core_control, "_current_sid", lambda: "S-1-5-21-100")
    monkeypatch.setattr(windows_core_control, "_check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args: None)
    monkeypatch.setattr(windows_job, "WindowsJob", FakeJob)
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda *args, **kwargs: events.append("runner.spawn") or FakeProcess(),
    )

    def stop_after_one(delay: float) -> None:
        raise StopLoop

    monkeypatch.setattr(service.time, "sleep", stop_after_one)
    with pytest.raises(StopLoop):
        service.run_watchdog(tmp_path / "serverfs.toml")
    assert events == [
        "job.open",
        "runner.spawn",
        "job.assign",
        "runner.kill",
        "runner.wait",
        "job.close",
    ]
