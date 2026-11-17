"""macOS v0.25 independently scoped Bridge lifecycle, no live LaunchAgent use."""

from __future__ import annotations

import pytest

from serverfs_mcp import darwin_service


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("bridge_start", "register:agent-bridge"),
        ("bridge_stop", "unregister:agent-bridge"),
    ],
)
def test_owned_bridge_only_changes_bridge(
    monkeypatch: pytest.MonkeyPatch, method: str, expected: str
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_load_manifest_document",
        lambda: {
            "services": {"agent-bridge": {}, "http-origin": {}, "cloudflare": {}, "tunnel": {}}
        },
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(f"unregister:{service}") or {service: "not-registered"},
    )
    getattr(darwin_service, method)()
    assert events == [expected]


def test_bridge_restart_does_not_touch_ingress_or_core(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_load_manifest_document",
        lambda: {"services": {"agent-bridge": {}, "tunnel": {}, "http-origin": {}}},
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(f"stop:{service}") or {service: "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: events.append(f"start:{service}") or {service: "enabled"},
    )
    assert darwin_service.bridge_restart() == {"agent-bridge": "enabled"}
    assert events == ["stop:agent-bridge", "start:agent-bridge"]


@pytest.mark.parametrize("method", ["bridge_start", "bridge_stop", "bridge_restart"])
def test_shared_or_disabled_client_never_controls_bridge(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(
        darwin_service, "_load_manifest_document", lambda: {"services": {"http-origin": {}}}
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda _: pytest.fail("bridge-less Core must never register a Bridge"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda _: pytest.fail("bridge-less Core must never unregister a Bridge"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="not installed"):
        getattr(darwin_service, method)()
