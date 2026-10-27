from __future__ import annotations

import json
import subprocess

import pytest

from serverfs_mcp import cli, darwin_service


def _manifest(*services: str) -> dict[str, object]:
    return {"services": {service: {} for service in services}}


def test_restart_stops_all_services_before_registration_and_waits_for_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(f"stop:{service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service, "_wait_managed_job", lambda service: events.append(f"ready:{service}")
    )

    assert darwin_service.restart() == {"tunnel": "enabled"}
    assert events == ["stop:tunnel", "register:tunnel", "ready:tunnel"]


def test_restart_waits_for_every_stop_before_registering_any_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service, "_load_manifest_document", lambda: _manifest("tunnel", "agent-bridge")
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(f"stop:{service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_managed_job",
        lambda service: events.append(f"ready:{service}"),
    )
    monkeypatch.setattr(
        darwin_service, "_wait_bridge_health", lambda: events.append("health:agent-bridge")
    )

    darwin_service.restart()

    assert events == [
        "stop:tunnel",
        "stop:agent-bridge",
        "register:tunnel",
        "ready:tunnel",
        "register:agent-bridge",
        "ready:agent-bridge",
        "health:agent-bridge",
    ]


def test_restart_accepts_manifest_without_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: calls.append(f"stop:{service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: calls.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda service: 123)
    monkeypatch.setattr(
        darwin_service,
        "_wait_bridge_health",
        lambda: pytest.fail("absent bridge must not be probed"),
    )

    assert darwin_service.restart() == {"tunnel": "enabled"}
    assert calls == ["stop:tunnel", "register:tunnel"]


@pytest.mark.parametrize("state", ["requires-approval", "unexpected", None])
def test_start_fails_closed_when_registration_is_not_enabled(
    monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))
    monkeypatch.setattr(darwin_service, "_register", lambda _service: {"tunnel": state})
    monkeypatch.setattr(
        darwin_service,
        "_wait_managed_job",
        lambda _service: pytest.fail("must not wait after failed registration"),
    )

    with pytest.raises(darwin_service.DarwinServiceError):
        darwin_service.start()


def test_readiness_failure_propagates_from_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))
    monkeypatch.setattr(darwin_service, "_unregister_and_wait", lambda _service: None)
    monkeypatch.setattr(darwin_service, "_register", lambda _service: {"tunnel": "enabled"})

    def fail_readiness(_service: str) -> None:
        raise darwin_service.DarwinServiceError("not ready")

    monkeypatch.setattr(darwin_service, "_wait_managed_job", fail_readiness)
    with pytest.raises(darwin_service.DarwinServiceError, match="not ready"):
        darwin_service.restart()


def test_bridge_health_failure_propagates_from_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        darwin_service, "_load_manifest_document", lambda: _manifest("agent-bridge")
    )
    monkeypatch.setattr(darwin_service, "_unregister_and_wait", lambda _service: None)
    monkeypatch.setattr(darwin_service, "_register", lambda _service: {"agent-bridge": "enabled"})
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda _service: 123)

    def fail_health() -> None:
        raise darwin_service.DarwinServiceError("unhealthy")

    monkeypatch.setattr(darwin_service, "_wait_bridge_health", fail_health)
    with pytest.raises(darwin_service.DarwinServiceError, match="unhealthy"):
        darwin_service.restart()


def test_restart_service_stops_selected_service_and_waits_for_bridge_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(f"stop:{service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_managed_job",
        lambda service: events.append(f"ready:{service}"),
    )
    monkeypatch.setattr(darwin_service, "_wait_bridge_health", lambda: events.append("health"))

    assert darwin_service.restart_service("agent-bridge") == {"agent-bridge": "enabled"}
    assert events == ["stop:agent-bridge", "register:agent-bridge", "ready:agent-bridge", "health"]


def test_register_service_waits_for_bridge_readiness_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_managed_job",
        lambda service: events.append(f"ready:{service}"),
    )
    monkeypatch.setattr(darwin_service, "_wait_bridge_health", lambda: events.append("health"))

    assert darwin_service.register_service("agent-bridge") == {"agent-bridge": "enabled"}
    assert events == ["register:agent-bridge", "ready:agent-bridge", "health"]


def test_restart_propagates_stop_failure_without_registering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))

    def fail_stop(_service: str) -> None:
        raise darwin_service.DarwinServiceError("old process did not exit")

    monkeypatch.setattr(darwin_service, "_unregister_and_wait", fail_stop)
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda _service: pytest.fail("register must follow a complete stop phase"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="old process did not exit"):
        darwin_service.restart()


def test_cli_tunnel_only_restart_stays_synchronous(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    events: list[str] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest("tunnel"))
    monkeypatch.setattr(
        darwin_service,
        "restart",
        lambda: events.append("restart") or {"tunnel": "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "schedule_handover",
        lambda *_args, **_kwargs: pytest.fail("Tunnel-only restart must not hand over"),
    )

    assert cli.cmd_service(SimpleNamespace(service_command="restart")) == 0
    assert events == ["restart"]


def test_cli_bridge_restart_schedules_manifest_only_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        darwin_service, "_load_manifest_document", lambda: _manifest("tunnel", "agent-bridge")
    )
    monkeypatch.setattr(
        darwin_service,
        "schedule_handover",
        lambda *args: scheduled.append(args),
    )
    monkeypatch.setattr(
        darwin_service, "restart", lambda: pytest.fail("parent must not restart Bridge inline")
    )

    assert cli.cmd_service(SimpleNamespace(service_command="restart")) == 0
    assert scheduled == [("restart",)]


def test_handover_restart_child_runs_closure_and_persists_pending_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    events: list[object] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        darwin_service, "restart", lambda: events.append("restart") or {"tunnel": "enabled"}
    )
    monkeypatch.setattr(
        darwin_service,
        "write_handover_result",
        lambda **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(darwin_service, "schedule_handover_cleanup", lambda: None)
    registrations: list[object] = []
    monkeypatch.setattr(cli.atexit, "register", lambda callback: registrations.append(callback))

    assert cli.cmd_service(SimpleNamespace(service_command="restart", _handover_child=True)) == 0
    assert events == ["restart", {"ok": True, "detail": "restarted", "complete": False}]
    assert len(registrations) == 1
    registrations[0]()


def test_agent_bridge_handover_child_restarts_only_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    events: list[object] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        darwin_service,
        "restart_service",
        lambda service: events.append(service) or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "write_handover_result",
        lambda **kwargs: events.append(kwargs),
    )
    callbacks: list[object] = []
    monkeypatch.setattr(cli.atexit, "register", lambda callback: callbacks.append(callback))

    assert (
        cli.cmd_agent_bridge(
            SimpleNamespace(
                agent_command="restart", config=None, env_file=None, _handover_child=True
            )
        )
        == 0
    )
    assert events == [
        darwin_service.BRIDGE_SERVICE,
        {"ok": True, "detail": "agent-bridge restarted", "complete": False},
    ]
    assert len(callbacks) == 1


def test_bridge_handover_job_uses_product_owned_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "ServerFS" / "service-manager"
    monkeypatch.setattr(darwin_service, "service_home", lambda: home)
    monkeypatch.setattr(darwin_service, "_bootout_handover_job", lambda: None)
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli.os, "getuid", lambda: 501)
    monkeypatch.setattr(darwin_service.sys, "executable", "/usr/bin/python3")

    darwin_service.schedule_handover("bridge-restart")

    document = darwin_service.plistlib.loads(darwin_service.handover_plist_path().read_bytes())
    assert document["WorkingDirectory"] == str(home.resolve())
    assert document["ProgramArguments"][3:] == [
        "agent-bridge",
        "restart",
        "--_handover-child",
    ]


def test_early_handover_install_failure_still_schedules_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from types import SimpleNamespace

    config = tmp_path / "serverfs.toml"
    env_file = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    events: list[object] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        darwin_service, "write_handover_result", lambda **kwargs: events.append(kwargs)
    )
    monkeypatch.setattr(
        darwin_service,
        "schedule_handover_cleanup",
        lambda: events.append("cleanup"),
    )
    callbacks: list[object] = []
    monkeypatch.setattr(cli.atexit, "register", lambda callback: callbacks.append(callback))

    code = cli.cmd_service(
        SimpleNamespace(
            service_command="install",
            config=config,
            env_file=env_file,
            _handover_child=True,
            _handover_roots="not-a-pid",
        )
    )
    assert code == 2
    assert events == [
        {"ok": False, "detail": "invalid frozen Tunnel root PID list", "complete": False}
    ]
    assert len(callbacks) == 1
    callbacks[0]()
    assert events[-1] == "cleanup"


def test_cleanup_finalizes_success_only_after_launchd_and_plist_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "service-manager"
    home.mkdir()
    monkeypatch.setattr(darwin_service, "service_home", lambda: home)
    darwin_service.write_handover_result(ok=True, detail="restarted", complete=False)
    plist = darwin_service.handover_plist_path()
    plist.write_text("plist", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        if command[1] == "print":
            return subprocess.CompletedProcess(command, 113, stdout="", stderr="not found")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(darwin_service.subprocess, "run", run)

    darwin_service.cleanup_handover_job()

    result = json.loads(darwin_service.handover_result_path().read_text(encoding="utf-8"))
    assert result["complete"] is True
    assert result["ok"] is True
    assert calls[0][1] == "bootout"
    assert calls[1][1] == "print"
    assert not plist.exists()


def test_agent_bridge_restart_syncs_then_hands_off_only_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from types import SimpleNamespace

    config = tmp_path / "serverfs.toml"
    config.write_text("", encoding="utf-8")
    events: list[object] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _path: ([], SimpleNamespace(bridge_required=True)),
    )
    from serverfs_mcp import darwin_bridge_policy

    monkeypatch.setattr(darwin_bridge_policy, "sync_policy", lambda *_args: events.append("sync"))
    monkeypatch.setattr(
        darwin_service,
        "schedule_handover",
        lambda *args: events.append(args),
    )
    monkeypatch.setattr(
        darwin_service,
        "restart_service",
        lambda *_args: pytest.fail("parent must not restart the Bridge inline"),
    )

    assert (
        cli.cmd_agent_bridge(
            SimpleNamespace(
                agent_command="restart", config=config, env_file=None, _handover_child=False
            )
        )
        == 0
    )
    assert events == ["sync", ("bridge-restart",)]


def test_cleanup_failure_preserves_pending_operation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "service-manager"
    home.mkdir()
    monkeypatch.setattr(darwin_service, "service_home", lambda: home)
    darwin_service.write_handover_result(ok=False, detail="Bridge health failed", complete=False)
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0 if command[1] == "print" else 0, stdout="loaded", stderr=""
        ),
    )

    with pytest.raises(darwin_service.DarwinServiceError, match="remains loaded"):
        darwin_service.cleanup_handover_job()

    result = json.loads(darwin_service.handover_result_path().read_text(encoding="utf-8"))
    assert result["complete"] is True
    assert result["ok"] is False
    assert "Bridge health failed" in result["detail"]
    assert "cleanup failed" in result["detail"]


def test_handover_status_exposes_bounded_final_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "service-manager"
    home.mkdir()
    monkeypatch.setattr(darwin_service, "service_home", lambda: home)
    darwin_service.write_handover_result(ok=True, detail="restarted")
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 113, stdout="", stderr=""),
    )

    evidence = darwin_service._handover_status()

    assert evidence["complete"] is True
    assert evidence["ok"] is True
    assert evidence["detail"] == "restarted"
    assert isinstance(evidence["timestamp"], float)
    assert evidence["job_loaded"] is False
    assert evidence["plist_exists"] is False


def test_native_service_status_serializes_handover_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from types import SimpleNamespace

    evidence = {
        "complete": True,
        "ok": False,
        "detail": "Bridge health failed",
        "timestamp": 123.0,
        "job_loaded": False,
        "plist_exists": False,
    }
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(darwin_service, "status", lambda: {"handover": evidence})

    assert cli.cmd_service(SimpleNamespace(service_command="status", status_format="native")) == 0
    assert json.loads(capsys.readouterr().err)["handover"] == evidence


def test_cleanup_helper_start_failure_persists_final_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    home = tmp_path / "service-manager"
    home.mkdir()
    monkeypatch.setattr(darwin_service, "service_home", lambda: home)
    darwin_service.write_handover_result(ok=True, detail="restarted", complete=False)

    def fail_start(*_args, **_kwargs):
        raise OSError("spawn refused")

    monkeypatch.setattr(darwin_service.subprocess, "Popen", fail_start)
    with pytest.raises(darwin_service.DarwinServiceError, match="cleanup could not be started"):
        darwin_service.schedule_handover_cleanup()

    result = json.loads(darwin_service.handover_result_path().read_text(encoding="utf-8"))
    assert result["complete"] is True
    assert result["ok"] is False
    assert "restarted" in result["detail"]
    assert "cleanup helper could not start" in result["detail"]
