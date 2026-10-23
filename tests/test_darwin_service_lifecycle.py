from __future__ import annotations

import pytest

from serverfs_mcp import darwin_service


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
