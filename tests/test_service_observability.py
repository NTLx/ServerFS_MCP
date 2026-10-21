from __future__ import annotations

import json
from datetime import UTC, datetime

from serverfs_mcp import cli
from serverfs_mcp.observability import Projection
from serverfs_mcp.service_observability import (
    darwin_common_projection,
    render_common_human,
    render_common_json,
    windows_common_projection,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_windows_projection_conservative_tunnel_and_aggregates() -> None:
    healthy = {
        "registered": True,
        "manifest_installed": True,
        "task_valid": True,
        "watchdog_alive": True,
        "runner_alive": True,
        "last_tunnel_started_at": 20,
    }
    snapshot, policy = windows_common_projection(healthy, observed_at=NOW)
    assert snapshot.aggregate(policy, Projection.READINESS).health.value == "healthy"
    down = {**healthy, "last_tunnel_exit_at": 20}
    snapshot, policy = windows_common_projection(down, observed_at=NOW)
    assert snapshot.aggregate(policy, Projection.READINESS).health.value == "unavailable"
    unknown = {key: value for key, value in healthy.items() if not key.startswith("last_tunnel")}
    snapshot, policy = windows_common_projection(unknown, observed_at=NOW)
    assert snapshot.aggregate(policy, Projection.READINESS).health.value == "unknown"


def test_darwin_projection_states_are_conservative() -> None:
    base = {
        "bundle_valid": True,
        "services": {"tunnel": "started"},
        "processes": {"tunnel": {"pid": 21}},
    }
    cases = [
        (base, "healthy"),
        ({**base, "unmanaged_tunnel_pids": [22]}, "degraded"),
        ({"bundle_valid": True, "services": {"tunnel": "not-installed"}}, "unavailable"),
        ({"bundle_valid": True, "services": {"tunnel": "future-state"}}, "unknown"),
    ]
    for native, expected in cases:
        snapshot, policy = darwin_common_projection(native, observed_at=NOW)
        assert snapshot.aggregate(policy, Projection.HEALTH).health.value == expected


def test_renderers_sanitize_native_values_and_agree() -> None:
    native = {
        "registered": True,
        "manifest_installed": True,
        "task_valid": True,
        "watchdog_alive": True,
        "runner_alive": True,
        "last_tunnel_started_at": 8,
        "task_name": "TASK_SENTINEL",
        "task_executable": "PATH_SENTINEL",
        "watchdog_pid": 123456,
        "state": "RAW_STATE_SENTINEL",
    }
    snapshot, policy = windows_common_projection(native, observed_at=NOW)
    document = json.loads(render_common_json(snapshot, policy))
    human = render_common_human(snapshot, policy)
    encoded = json.dumps(document) + human
    for sentinel in ("TASK_SENTINEL", "PATH_SENTINEL", "123456", "RAW_STATE_SENTINEL"):
        assert sentinel not in encoded
    for projection in Projection:
        assert document[projection.value] == snapshot.aggregate(policy, projection).as_dict()
        assert f"{projection.value}: {document[projection.value]['health']} " in human
    assert "pid" not in human


def test_service_status_native_default_is_unchanged(monkeypatch, capsys) -> None:
    payload = {"task_name": "native-sentinel", "registered": True}
    monkeypatch.setattr(cli.sys, "platform", "win32")
    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "status", lambda: payload)
    assert cli.main(["service", "status"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == json.dumps(payload, sort_keys=True) + "\n"


def test_service_status_common_json_uses_stderr_and_is_sanitized(monkeypatch, capsys) -> None:
    payload = {
        "registered": True,
        "manifest_installed": True,
        "task_valid": True,
        "watchdog_alive": True,
        "runner_alive": True,
        "last_tunnel_started_at": 20,
        "task_name": "TASK_SENTINEL",
        "task_executable": "PATH_SENTINEL",
        "watchdog_pid": 123456,
    }
    monkeypatch.setattr(cli.sys, "platform", "win32")
    from serverfs_mcp import windows_service

    monkeypatch.setattr(windows_service, "status", lambda: payload)
    assert cli.main(["service", "status", "--format", "common-json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    document = json.loads(captured.err)
    assert document["liveness"]["health"] == "healthy"
    assert document["readiness"]["health"] == "healthy"
    encoded = json.dumps(document)
    for sentinel in ("TASK_SENTINEL", "PATH_SENTINEL", "123456"):
        assert sentinel not in encoded


def test_service_status_common_human_uses_stderr_and_is_sanitized(monkeypatch, capsys) -> None:
    payload = {
        "bundle_valid": True,
        "services": {"tunnel": "started", "agent-bridge": "RAW_STATE_SENTINEL"},
        "processes": {"tunnel": {"pid": 21}, "agent-bridge": {"pid": 777}},
        "app_path": "PATH_SENTINEL",
        "log_dir": "LOG_SENTINEL",
    }
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    from serverfs_mcp import darwin_service

    monkeypatch.setattr(darwin_service, "status", lambda: payload)
    assert cli.main(["service", "status", "--format", "common-human"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "role: native_service" in captured.err
    assert "platform: darwin" in captured.err
    assert "liveness: healthy (all_required_healthy)" in captured.err
    assert "tunnel: enabled healthy observed (ok)" in captured.err
    for sentinel in ("RAW_STATE_SENTINEL", "PATH_SENTINEL", "LOG_SENTINEL", "777", "21"):
        assert sentinel not in captured.err
