from __future__ import annotations

import json
from datetime import UTC, datetime

from serverfs_mcp import cli
from serverfs_mcp.observability import Projection
from serverfs_mcp.service_observability import (
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
