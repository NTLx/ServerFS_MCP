"""W2 source contract: scoped active Core/connector lifecycle without OS effects.

All Task Scheduler calls, process status, listeners, config and credentials are
synthetic. No real task, Core, connector or Bridge is started by this suite.
"""

from __future__ import annotations

import subprocess

import pytest

from serverfs_mcp import windows_connector_control as connector
from serverfs_mcp import windows_connector_task_spec as connector_tasks
from serverfs_mcp import windows_core_control as core
from serverfs_mcp import windows_core_task_spec as core_tasks


def _core_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, ...]], dict]:
    state = {"enabled": False, "alive": False}
    events: list[tuple[str, ...]] = []
    manifest = {
        "config_path": "C:/private/serverfs.toml",
        "auth_file": "C:/private/core-auth.env",
        "port": 49876,
        "hostname": "mcp.example.test",
        "sid": "S-1-5-21-100-200-300-1000",
    }
    monkeypatch.setattr(
        core,
        "_require_owned",
        lambda: (manifest, "enabled" if state["enabled"] else "disabled"),
    )
    monkeypatch.setattr(core, "_task_enabled", lambda xml: xml == "enabled")
    monkeypatch.setattr(
        core,
        "status",
        lambda: {
            "task_valid": True,
            "enabled": state["enabled"],
            "core_alive": state["alive"],
            "listener_owner": "verified" if state["alive"] else "not_verified",
        },
    )
    monkeypatch.setattr(core, "_require_previous_generation_stopped", lambda: None)
    monkeypatch.setattr(core.runtime, "validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core,
        "_wait_for_core",
        lambda *, alive, timeout_seconds=20: {
            "core_alive": alive,
            "listener_owner": "verified" if alive else "not_verified",
        },
    )

    def task_call(*args: str) -> subprocess.CompletedProcess[bytes]:
        events.append(args)
        if args[0] == "/Change":
            state["enabled"] = args[-1] == "/ENABLE"
        if args[0] == "/Run":
            state["alive"] = True
        if args[0] == "/End":
            state["alive"] = False
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(core, "_task_call", task_call)
    monkeypatch.setattr(core, "_expect_task_success", lambda *args: task_call(*args))
    return events, state


def test_core_starts_and_stops_only_its_owned_task(monkeypatch: pytest.MonkeyPatch) -> None:
    events, state = _core_fixture(monkeypatch)
    assert core.start()["core_alive"] is True
    assert state == {"enabled": True, "alive": True}
    assert events == [
        ("/Change", "/TN", core_tasks.TASK_NAME, "/ENABLE"),
        ("/Run", "/TN", core_tasks.TASK_NAME),
    ]
    assert core.start()["core_alive"] is True
    assert len(events) == 2  # idempotent active start
    assert core.stop()["core_alive"] is False
    assert state == {"enabled": False, "alive": False}
    assert events[-2:] == [
        ("/Change", "/TN", core_tasks.TASK_NAME, "/DISABLE"),
        ("/End", "/TN", core_tasks.TASK_NAME),
    ]
    assert core.stop()["core_alive"] is False
    assert len(events) == 4  # idempotent inactive stop
    assert all(core_tasks.TASK_NAME in args for args in events)


def test_core_never_starts_when_port_or_policy_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, _state = _core_fixture(monkeypatch)

    def rejected(*args: object, **kwargs: object) -> None:
        raise ValueError("private policy check failed")

    monkeypatch.setattr(core.runtime, "validate_runtime", rejected)
    with pytest.raises(ValueError, match="policy check"):
        core.start()
    assert events == []


def test_core_restarts_via_scoped_stop_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    events, _state = _core_fixture(monkeypatch)
    core.start()
    events.clear()
    assert core.restart()["core_alive"] is True
    assert events == [
        ("/Change", "/TN", core_tasks.TASK_NAME, "/DISABLE"),
        ("/End", "/TN", core_tasks.TASK_NAME),
        ("/Change", "/TN", core_tasks.TASK_NAME, "/ENABLE"),
        ("/Run", "/TN", core_tasks.TASK_NAME),
    ]


@pytest.mark.parametrize("kind", ("cloudflare", "openai"))
def test_connector_starts_stops_and_restarts_only_its_named_task(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"enabled": False, "alive": False}
    events: list[tuple[str, ...]] = []
    policy = {"core_port": 49876, "core_hostname": "mcp.example.test"}
    monkeypatch.setattr(
        connector, "_owned", lambda actual: ({}, "enabled" if state["enabled"] else "disabled")
    )
    monkeypatch.setattr(connector, "_enabled", lambda xml: xml == "enabled")
    monkeypatch.setattr(
        connector,
        "status",
        lambda actual: {"task_valid": True, "owner_alive": state["alive"]},
    )
    monkeypatch.setattr(connector, "_previous_gone", lambda actual: None)
    monkeypatch.setattr(connector.runtime, "load_policy", lambda actual: policy)
    monkeypatch.setattr(connector.runtime, "validate_runtime_inputs", lambda *args: None)
    from serverfs_mcp import windows_core_listener

    observed_core: list[tuple[int, str]] = []
    monkeypatch.setattr(
        windows_core_listener,
        "prove_core_listener",
        lambda *, expected_port, expected_hostname: observed_core.append(
            (expected_port, expected_hostname)
        ),
    )
    monkeypatch.setattr(
        connector,
        "_wait_owner",
        lambda actual, *, alive, timeout_seconds=20: {"owner_alive": alive},
    )

    def task_call(*args: str) -> subprocess.CompletedProcess[bytes]:
        events.append(args)
        if args[0] == "/Change":
            state["enabled"] = args[-1] == "/ENABLE"
        if args[0] == "/Run":
            state["alive"] = True
        if args[0] == "/End":
            state["alive"] = False
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(connector, "_call", task_call)
    monkeypatch.setattr(connector, "_expect", lambda *args: task_call(*args))
    name = connector_tasks.TASK_NAMES[kind]
    assert connector.start(kind)["owner_alive"]
    assert connector.start(kind)["owner_alive"]
    assert events == [
        ("/Change", "/TN", name, "/ENABLE"),
        ("/Run", "/TN", name),
    ]
    assert observed_core == [(49876, "mcp.example.test")]
    assert not connector.stop(kind)["owner_alive"]
    assert connector.restart(kind)["owner_alive"]
    assert events == [
        ("/Change", "/TN", name, "/ENABLE"),
        ("/Run", "/TN", name),
        ("/Change", "/TN", name, "/DISABLE"),
        ("/End", "/TN", name),
        ("/Change", "/TN", name, "/ENABLE"),
        ("/Run", "/TN", name),
    ]
    assert all(name in event for event in events)


def test_connector_requires_a_verified_core_before_enabling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = "openai"
    monkeypatch.setattr(connector, "_owned", lambda actual: ({}, "disabled"))
    monkeypatch.setattr(connector, "_enabled", lambda _xml: False)
    monkeypatch.setattr(
        connector, "status", lambda actual: {"task_valid": True, "owner_alive": False}
    )
    monkeypatch.setattr(connector, "_previous_gone", lambda actual: None)
    monkeypatch.setattr(
        connector.runtime,
        "load_policy",
        lambda actual: {"core_port": 49876, "core_hostname": "mcp.example.test"},
    )
    monkeypatch.setattr(connector.runtime, "validate_runtime_inputs", lambda *args: None)
    from serverfs_mcp import windows_core_listener

    monkeypatch.setattr(
        windows_core_listener,
        "prove_core_listener",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("Core listener unavailable")),
    )
    monkeypatch.setattr(connector, "_call", lambda *args: pytest.fail("no Task Scheduler mutation"))
    monkeypatch.setattr(
        connector, "_expect", lambda *args: pytest.fail("no Task Scheduler mutation")
    )
    with pytest.raises(ValueError, match="listener unavailable"):
        connector.start(kind)


def test_core_failed_activation_reverts_its_own_scheduler_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, state = _core_fixture(monkeypatch)
    monkeypatch.setattr(
        core,
        "_wait_for_core",
        lambda **kwargs: (_ for _ in ()).throw(core.CoreControlError("listener failed")),
    )
    with pytest.raises(core.CoreControlError, match="listener failed"):
        core.start()
    assert not state["enabled"]
    assert events == [
        ("/Change", "/TN", core_tasks.TASK_NAME, "/ENABLE"),
        ("/Run", "/TN", core_tasks.TASK_NAME),
        ("/Change", "/TN", core_tasks.TASK_NAME, "/DISABLE"),
    ]


def test_connector_failed_run_reverts_only_its_own_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = "cloudflare"
    state = {"enabled": False}
    events: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        connector,
        "_owned",
        lambda _kind: ({}, "enabled" if state["enabled"] else "disabled"),
    )
    monkeypatch.setattr(connector, "_enabled", lambda xml: xml == "enabled")
    monkeypatch.setattr(
        connector,
        "status",
        lambda _kind: {"task_valid": True, "owner_alive": False},
    )
    monkeypatch.setattr(connector, "_previous_gone", lambda _kind: None)
    monkeypatch.setattr(
        connector.runtime,
        "load_policy",
        lambda _kind: {"core_port": 49876, "core_hostname": "mcp.example.test"},
    )
    monkeypatch.setattr(connector.runtime, "validate_runtime_inputs", lambda *args: None)
    from serverfs_mcp import windows_core_listener

    monkeypatch.setattr(windows_core_listener, "prove_core_listener", lambda **kwargs: None)

    def scheduler(*args: str) -> None:
        events.append(args)
        if args[0] == "/Run":
            raise connector.ConnectorControlError("Task Scheduler run failed")
        if args[0] == "/Change":
            state["enabled"] = args[-1] == "/ENABLE"

    monkeypatch.setattr(connector, "_expect", scheduler)
    with pytest.raises(connector.ConnectorControlError, match="run failed"):
        connector.start(kind)
    assert not state["enabled"]
    assert events == [
        ("/Change", "/TN", connector_tasks.TASK_NAMES[kind], "/ENABLE"),
        ("/Run", "/TN", connector_tasks.TASK_NAMES[kind]),
        ("/Change", "/TN", connector_tasks.TASK_NAMES[kind], "/DISABLE"),
    ]
