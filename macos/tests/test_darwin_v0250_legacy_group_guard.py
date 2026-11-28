"""v0.25.0 macOS fail-closed guard for dangerous legacy group lifecycle."""

from __future__ import annotations

import pytest

from serverfs_mcp import darwin_service


@pytest.mark.parametrize("operation", ["start", "stop", "restart", "uninstall"])
@pytest.mark.parametrize(
    ("service", "owner_field", "owner_value"),
    [
        ("http-origin", "origin_owner", "independent-core"),
        ("cloudflare", "connector_owner", "independent-connector"),
        ("tunnel", "connector_owner", "independent-openai-http"),
    ],
)
def test_legacy_group_operation_refuses_independent_owned_service_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    service: str,
    owner_field: str,
    owner_value: str,
) -> None:
    document = {"services": {service: {owner_field: owner_value}, "agent-bridge": {}}}
    monkeypatch.setattr(
        darwin_service,
        "_load_manifest_document",
        lambda **_kwargs: document,
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda owner: pytest.fail(f"must not stop {owner}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda owner: pytest.fail(f"must not register {owner}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_atomic_private_write",
        lambda path, payload: pytest.fail(f"must not write {path}"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="explicit --component"):
        getattr(darwin_service, operation)()


def test_legacy_group_operation_keeps_original_unmarked_manifest_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {"services": {"tunnel": {}, "agent-bridge": {}}}
    events: list[str] = []
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda **_kwargs: document)
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda svc: events.append(f"stop:{svc}") or {svc: "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda svc: events.append(f"start:{svc}") or {svc: "enabled"},
    )

    assert darwin_service.stop() == {
        "tunnel": "not-registered",
        "agent-bridge": "not-registered",
    }
    assert events == ["stop:tunnel", "stop:agent-bridge"]
    events.clear()
    assert darwin_service.start() == {"tunnel": "enabled", "agent-bridge": "enabled"}
    assert events == ["start:tunnel", "start:agent-bridge"]


def test_legacy_install_refuses_independent_core_before_touching_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    document = {"services": {"http-origin": {"origin_owner": "independent-core"}}}
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda **_kwargs: document)
    monkeypatch.setattr(
        darwin_service,
        "_prepare_install",
        lambda *args, **kwargs: pytest.fail("must not prepare legacy install"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="explicit --component"):
        darwin_service.install(
            Path("/unused/serverfs.toml"),
            Path("/unused/tunnel.env"),
            bridge_enabled=False,
        )
