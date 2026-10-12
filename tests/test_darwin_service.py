from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from serverfs_mcp import darwin_service


def test_bundled_bridge_label_is_distinct_from_legacy_identity() -> None:
    assert darwin_service.BRIDGE_LABEL == "com.ntlx.serverfs.bridge"
    assert darwin_service.LEGACY_BRIDGE_LABEL == "com.ntlx.serverfs.agent-bridge"
    assert darwin_service.BRIDGE_LABEL != darwin_service.LEGACY_BRIDGE_LABEL
    assert (
        darwin_service._SERVICE_LABELS[darwin_service.BRIDGE_SERVICE] == darwin_service.BRIDGE_LABEL
    )


def test_service_manager_build_contract_uses_bundled_bridge_plist() -> None:
    root = Path(__file__).resolve().parents[1] / "native" / "macos" / "service_manager"
    build_script = (root / "build.sh").read_text(encoding="utf-8")

    assert (root / "LaunchAgents" / "com.ntlx.serverfs.bridge.plist").is_file()
    assert not (root / "LaunchAgents" / "com.ntlx.serverfs.agent-bridge.plist").exists()
    assert "LaunchAgents/com.ntlx.serverfs.bridge.plist" in build_script
    assert "LaunchAgents/com.ntlx.serverfs.agent-bridge.plist" not in build_script


def test_read_env_file_supports_export_and_quotes(tmp_path: Path) -> None:
    env = tmp_path / "service.env"
    env.write_text(
        "# comment\n"
        "export CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n"
        'CONTROL_PLANE_API_KEY="secret value"\n'
        'SERVERFS_PROXY_PASSWORD="p\\\\a\\"ss"\n',
        encoding="utf-8",
    )

    values = darwin_service._read_env_file(env)

    assert values["CONTROL_PLANE_API_KEY"] == "secret value"
    assert values["SERVERFS_PROXY_PASSWORD"] == 'p\\a"ss'


def test_read_env_file_rejects_duplicates(tmp_path: Path) -> None:
    env = tmp_path / "service.env"
    env.write_text("A=1\nA=2\n", encoding="utf-8")

    with pytest.raises(darwin_service.DarwinServiceError, match="duplicate A"):
        darwin_service._read_env_file(env)


def test_private_runtime_files_are_0600_and_proxy_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "service-manager")
    values = {
        "CONTROL_PLANE_API_KEY": "secret",
        "SERVERFS_PROXY_HOST": "127.0.0.1",
        "SERVERFS_PROXY_PORT": "7897",
        "SERVERFS_PROXY_PASSWORD": 'a b#c\\d"e',
        "SERVERFS_JEV_API_KEY": "must-not-reach-tunnel",
        "SERVERFS_FILE_INGRESS_ENABLED": "true",
        "UNRELATED": "ignored",
    }

    darwin_service._write_private_runtime_files(values)

    environment_path = darwin_service.tunnel_environment_path()
    proxy_path = darwin_service.tunnel_proxy_env_path()
    assert environment_path.stat().st_mode & 0o777 == 0o600
    assert proxy_path.stat().st_mode & 0o777 == 0o600
    environment = json.loads(environment_path.read_text())
    assert environment["CONTROL_PLANE_API_KEY"] == "secret"
    assert environment["SERVERFS_FILE_INGRESS_ENABLED"] == "true"
    assert "SERVERFS_JEV_API_KEY" not in environment
    assert "UNRELATED" not in environment
    from serverfs_mcp.native_tunnel import parse_proxy_env_file

    parsed = parse_proxy_env_file(proxy_path)
    assert parsed["SERVERFS_PROXY_PASSWORD"] == 'a b#c\\d"e'


def test_manifest_keeps_secret_out_of_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "service-manager")
    target = tmp_path / "python-target"
    target.write_text("", encoding="utf-8")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(target)
    monkeypatch.setattr(darwin_service.sys, "executable", str(venv_python))
    config = tmp_path / "serverfs.toml"
    config.write_text("", encoding="utf-8")
    values = {
        "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
        "CONTROL_PLANE_API_KEY": "top-secret",
    }
    darwin_service._write_private_runtime_files(values)

    darwin_service._write_manifest(config, values, bridge_enabled=False)

    document = json.loads(darwin_service.manifest_path().read_text())
    tunnel = document["services"]["tunnel"]
    assert tunnel["environment_file"] == str(darwin_service.tunnel_environment_path().resolve())
    assert tunnel["executable"] == str(venv_python.absolute())
    assert tunnel["executable"] != str(target.resolve())
    assert "top-secret" not in json.dumps(tunnel)
    assert "--api-key-file" not in tunnel["arguments"]


def test_unmanaged_tunnel_detection_excludes_managed_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        "101 1 /x/python -m serverfs_mcp.cli tunnel --config /x\n"
        "102 101 /x/tunnel-client run --mcp.command serverfs_mcp.supervisor\n"
        "103 102 /x/python -m serverfs_mcp.supervisor --config /x\n"
        "202 1 /x/serverfs tunnel --config /y\n"
        "203 202 /x/tunnel-client run --mcp.command serverfs_mcp.supervisor\n"
        "204 203 /x/python -m serverfs_mcp.supervisor --config /y\n"
        "205 204 /x/python -m serverfs_mcp.cli serve --config /y\n"
        "303 1 unrelated\n"
    )
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _service: 101)
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=""),
    )

    assert darwin_service.find_unmanaged_tunnel_pids() == [202, 203, 204, 205]


def test_unmanaged_tunnel_detection_finds_orphaned_client_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        "401 1 /x/tunnel-client run --mcp.command serverfs_mcp.supervisor\n"
        "402 401 /x/python -m serverfs_mcp.supervisor --config /x\n"
        "403 402 /x/python -m serverfs_mcp.cli serve --config /x\n"
    )
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _service: None)
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=""),
    )

    assert darwin_service.find_unmanaged_tunnel_pids() == [401, 402, 403]


def test_install_refuses_unmanaged_tunnel_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    config = tmp_path / "serverfs.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_roots", lambda: [42])
    monkeypatch.setattr("serverfs_mcp.darwin_platform.ensure_supported_darwin", lambda: None)

    with pytest.raises(darwin_service.DarwinServiceError, match="unmanaged v0.13.1 tunnel"):
        darwin_service.install(config, env, bridge_enabled=False)


def test_schedule_handover_is_one_shot_launchagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "service-manager")
    target = tmp_path / "python-target"
    target.write_text("", encoding="utf-8")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(target)
    monkeypatch.setattr(darwin_service.sys, "executable", str(venv_python))
    calls: list[list[str]] = []
    monkeypatch.setattr(darwin_service, "_bootout_handover_job", lambda: None)

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(darwin_service.subprocess, "run", fake_run)
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env.write_text("", encoding="utf-8")

    darwin_service.schedule_handover_install(config, env, [101, 202])

    document = darwin_service.plistlib.loads(darwin_service.handover_plist_path().read_bytes())
    assert document["Label"] == darwin_service.HANDOVER_LABEL
    assert document["KeepAlive"] is False
    assert document["RunAtLoad"] is False
    assert document["ProgramArguments"][0] == str(venv_python.absolute())
    assert document["ProgramArguments"][0] != str(target.resolve())
    assert "--_handover-child" in document["ProgramArguments"]
    assert document["ProgramArguments"][-1] == "101,202"
    assert calls[0][:3] == ["launchctl", "bootstrap", f"gui/{darwin_service.os.getuid()}"]
    assert calls[1] == ["launchctl", "kickstart", darwin_service._handover_label_target()]


def test_schedule_handover_allows_bridge_only_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "service-manager")
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(darwin_service.sys, "executable", str(python))
    monkeypatch.setattr(darwin_service, "_bootout_handover_job", lambda: None)
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env.write_text("", encoding="utf-8")

    darwin_service.schedule_handover_install(config, env, [])

    document = darwin_service.plistlib.loads(darwin_service.handover_plist_path().read_bytes())
    assert document["KeepAlive"] is False
    assert document["RunAtLoad"] is False
    assert document["ProgramArguments"][-2:] == ["--_handover-roots", ""]


def test_bridge_only_handover_prepares_without_terminating_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env.write_text("", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr("serverfs_mcp.darwin_platform.ensure_supported_darwin", lambda: None)
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_roots", lambda: [])
    monkeypatch.setattr(
        darwin_service,
        "_prepare_install",
        lambda *_args, **_kwargs: calls.append("prepare"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_terminate_frozen_unmanaged_tunnel",
        lambda roots: calls.append(f"terminate:{roots}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_activate_prepared_install",
        lambda **_kwargs: calls.append("activate") or {"tunnel": "enabled"},
    )

    result = darwin_service.install(
        config,
        env,
        bridge_enabled=True,
        expected_unmanaged_roots=[],
    )

    assert calls == ["prepare", "activate"]
    assert result == {"tunnel": "enabled"}


def test_handover_prepares_before_stopping_old_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env.write_text("", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr("serverfs_mcp.darwin_platform.ensure_supported_darwin", lambda: None)
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_roots", lambda: [42])
    monkeypatch.setattr(
        darwin_service,
        "_prepare_install",
        lambda *_args, **_kwargs: calls.append("prepare"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_terminate_frozen_unmanaged_tunnel",
        lambda roots: calls.append(f"terminate:{roots}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_activate_prepared_install",
        lambda **_kwargs: calls.append("activate") or {"tunnel": "enabled"},
    )

    result = darwin_service.install(
        config,
        env,
        bridge_enabled=False,
        expected_unmanaged_roots=[42],
    )

    assert calls == ["prepare", "terminate:[42]", "activate"]
    assert result == {"tunnel": "enabled"}


def test_manager_error_preserves_native_service_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "serverfs-service-manager"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: executable)
    payload = {
        "ok": False,
        "services": {"agent-bridge": "not-registered"},
        "errors": {"agent-bridge": "Operation not permitted"},
    }
    monkeypatch.setattr(
        darwin_service.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(darwin_service.DarwinServiceError) as captured:
        darwin_service._run_manager("register", "agent-bridge")

    message = str(captured.value)
    assert "agent-bridge: Operation not permitted" in message
    assert "states: agent-bridge=not-registered" in message


def test_status_separates_registration_and_process_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin_service.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(darwin_service.Path, "is_file", lambda self: True)
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(
        darwin_service,
        "native_status",
        lambda: {"tunnel": "enabled", "agent-bridge": "requires-approval"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_run_manager",
        lambda *args: {"ok": True, "version": "0.13.2"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_managed_job_pid",
        lambda service: 123 if service == "tunnel" else None,
    )
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_pids", lambda: [])

    snapshot = darwin_service.status()

    assert snapshot["bundle_valid"] is True
    assert snapshot["services"]["tunnel"] == "enabled"
    assert snapshot["processes"]["tunnel"]["pid"] == 123
    assert snapshot["services"]["agent-bridge"] == "requires-approval"
    assert snapshot["processes"]["agent-bridge"]["pid"] is None


def test_unregister_waits_for_managed_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_managed_job_pid",
        lambda service: calls.append(f"pid:{service}") or 31337,
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister",
        lambda service: calls.append(f"unregister:{service}") or {service: "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_unregistered",
        lambda service: calls.append(f"wait-state:{service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_process_exit",
        lambda pid, **kwargs: calls.append(f"wait-pid:{pid}:{kwargs['description']}"),
    )

    states = darwin_service._unregister_and_wait(darwin_service.BRIDGE_SERVICE)

    assert calls == [
        "pid:agent-bridge",
        "unregister:agent-bridge",
        "wait-state:agent-bridge",
        "wait-pid:31337:agent-bridge managed process",
    ]
    assert states == {"agent-bridge": "not-registered"}


def test_legacy_bridge_handover_waits_for_old_process_before_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy.plist"
    legacy.write_text("legacy", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(darwin_service, "legacy_bridge_plist_path", lambda: legacy)
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: calls.append(f"unregister:{service}") or {service: "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_launchd_job_pid",
        lambda label: calls.append(f"pid:{label}") or 4242,
    )
    monkeypatch.setattr(
        darwin_service, "_bootout_legacy_bridge", lambda: calls.append("bootout-legacy")
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_process_exit",
        lambda pid, **_kwargs: calls.append(f"wait-exit:{pid}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_stage_legacy_bridge_plist",
        lambda: calls.append("stage-legacy"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: calls.append(f"register:{service}") or {service: "enabled"},
    )

    darwin_service._migrate_legacy_bridge(bridge_enabled=True)

    assert calls == [
        "unregister:agent-bridge",
        f"pid:{darwin_service.LEGACY_BRIDGE_LABEL}",
        "bootout-legacy",
        "wait-exit:4242",
        "stage-legacy",
        "register:agent-bridge",
    ]
    assert legacy.exists()


def test_legacy_bridge_requires_approval_restores_legacy_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy.plist"
    legacy.write_text("legacy", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(darwin_service, "legacy_bridge_plist_path", lambda: legacy)
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: calls.append(f"unregister:{service}") or {service: "not-registered"},
    )
    monkeypatch.setattr(darwin_service, "_launchd_job_pid", lambda _label: None)
    monkeypatch.setattr(
        darwin_service, "_bootout_legacy_bridge", lambda: calls.append("bootout-legacy")
    )
    monkeypatch.setattr(
        darwin_service,
        "_stage_legacy_bridge_plist",
        lambda: calls.append("stage-legacy"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: calls.append(f"register:{service}") or {service: "requires-approval"},
    )
    monkeypatch.setattr(
        darwin_service, "_rollback_legacy_bridge", lambda: calls.append("rollback-legacy")
    )

    with pytest.raises(darwin_service.DarwinServiceError, match="requires approval"):
        darwin_service._migrate_legacy_bridge(bridge_enabled=True)

    assert calls == [
        "unregister:agent-bridge",
        "bootout-legacy",
        "stage-legacy",
        "register:agent-bridge",
        "unregister:agent-bridge",
        "rollback-legacy",
    ]


def test_activation_waits_for_bridge_process_before_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: calls.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_managed_job",
        lambda service: calls.append(f"wait:{service}") or 123,
    )
    monkeypatch.setattr(
        darwin_service,
        "legacy_bridge_plist_path",
        lambda: tmp_path / "absent.plist",
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_bridge_health",
        lambda: calls.append("bridge-health"),
    )
    monkeypatch.setattr(
        darwin_service,
        "native_status",
        lambda _service="all": {"tunnel": "enabled", "agent-bridge": "enabled"},
    )

    states = darwin_service._activate_prepared_install(bridge_enabled=True)

    assert calls == [
        "register:tunnel",
        "wait:tunnel",
        "register:agent-bridge",
        "wait:agent-bridge",
        "bridge-health",
    ]
    assert states["agent-bridge"] == "enabled"


def test_legacy_bridge_staging_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "LaunchAgents" / "legacy.plist"
    backup = tmp_path / "private" / "legacy.plist"
    legacy.parent.mkdir()
    legacy.write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(darwin_service, "legacy_bridge_plist_path", lambda: legacy)
    monkeypatch.setattr(darwin_service, "legacy_bridge_backup_path", lambda: backup)

    darwin_service._stage_legacy_bridge_plist()

    assert not legacy.exists()
    assert backup.read_text(encoding="utf-8") == "legacy"
    assert backup.stat().st_mode & 0o777 == 0o600

    darwin_service._restore_legacy_bridge_plist()

    assert legacy.read_text(encoding="utf-8") == "legacy"
    assert not backup.exists()


def test_reinstall_with_agent_disabled_unregisters_previous_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "serverfs.toml"
    env_file = tmp_path / ".env"
    config.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    candidate_parent = tmp_path / "candidate"
    candidate = candidate_parent / darwin_service.APP_NAME
    candidate.mkdir(parents=True)
    calls: list[str] = []

    monkeypatch.setattr("serverfs_mcp.darwin_platform.ensure_supported_darwin", lambda: None)
    monkeypatch.setattr(
        darwin_service,
        "_read_env_file",
        lambda _path: {
            "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
            "CONTROL_PLANE_API_KEY": "secret",
        },
    )
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_pids", lambda: [])
    monkeypatch.setattr(darwin_service, "_write_private_runtime_files", lambda _values: None)
    monkeypatch.setattr(
        darwin_service,
        "_write_manifest",
        lambda _config, _values, *, bridge_enabled: None,
    )
    monkeypatch.setattr(darwin_service, "_build_candidate_app", lambda: candidate)
    monkeypatch.setattr(darwin_service, "_install_candidate_app", lambda _candidate: candidate)
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: {service: "enabled"},
    )
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda _service: 123)
    monkeypatch.setattr(darwin_service, "legacy_bridge_plist_path", lambda: tmp_path / "absent")
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: calls.append(service) or {service: "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "native_status",
        lambda _service="all": {"tunnel": "enabled", "agent-bridge": "not-registered"},
    )

    states = darwin_service.install(config, env_file, bridge_enabled=False)

    assert calls == ["agent-bridge"]
    assert states["agent-bridge"] == "not-registered"
