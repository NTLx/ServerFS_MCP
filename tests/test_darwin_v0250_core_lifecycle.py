"""Source-only v0.25.0 tests for the existing macOS HTTP origin as independent Core."""

from __future__ import annotations

import pytest

from serverfs_mcp import darwin_service


def _manifest(arguments: list[str] | None = None) -> dict[str, object]:
    return {
        "services": {
            "http-origin": {
                "origin_owner": "independent-core",
                "arguments": arguments
                if arguments is not None
                else [
                    "-m",
                    "serverfs_mcp.cli",
                    "serve",
                    "--transport",
                    "streamable-http",
                    "--config",
                    "/private/serverfs.toml",
                    "--http-port",
                    "8765",
                ]
            },
            "cloudflare": {},
            "agent-bridge": {},
            "tunnel": {},
        }
    }


def test_core_start_registers_only_http_service_and_probes_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(darwin_service, "_load_manifest_document", _manifest)
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    monkeypatch.setattr(
        darwin_service,
        "_wait_http_origin",
        lambda port: events.append(f"probe:{port}"),
    )
    assert darwin_service.core_start() == {"http-origin": "enabled"}
    assert events == ["register:http-origin", "probe:8765"]


def test_core_stop_unregisters_only_http_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(darwin_service, "_load_manifest_document", _manifest)
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: calls.append(service) or {service: "not-registered"},
    )
    assert darwin_service.core_stop() == {"http-origin": "not-registered"}
    assert calls == ["http-origin"]


def test_core_restart_does_not_touch_connector_or_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "core_stop",
        lambda: calls.append("stop-core") or {"http-origin": "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "core_start",
        lambda: calls.append("start-core") or {"http-origin": "enabled"},
    )
    assert darwin_service.core_restart() == {"http-origin": "enabled"}
    assert calls == ["stop-core", "start-core"]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["-m", "serverfs_mcp.cli", "serve", "--transport", "stdio", "--http-port", "8765"],
        ["-m", "serverfs_mcp.cli", "serve", "--transport", "streamable-http"],
        [
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--http-port",
            "0",
        ],
        [
            "-m",
            "serverfs_mcp.cli",
            "serve",
            "--transport",
            "streamable-http",
            "--http-port",
            "invalid",
        ],
    ],
)
def test_core_start_refuses_invalid_manifest_before_os_action(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: _manifest(arguments))
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: pytest.fail(f"must not register {service}"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="Core"):
        darwin_service.core_start()


def test_core_start_refuses_absent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: {"services": {}})
    with pytest.raises(darwin_service.DarwinServiceError, match="not installed"):
        darwin_service.core_start()


def test_core_operations_refuse_legacy_origin_without_owner_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _manifest()
    del legacy["services"]["http-origin"]["origin_owner"]
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: legacy)
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: pytest.fail(f"must not register {service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: pytest.fail(f"must not unregister {service}"),
    )
    for action in (darwin_service.core_start, darwin_service.core_stop):
        with pytest.raises(darwin_service.DarwinServiceError, match="not installed"):
            action()


def test_old_cloudflare_pair_operations_refuse_independent_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", _manifest)
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: pytest.fail(f"must not unregister {service}"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: pytest.fail(f"must not register {service}"),
    )
    for action in (darwin_service.cloudflare_start, darwin_service.cloudflare_stop):
        with pytest.raises(darwin_service.DarwinServiceError, match="independent Core"):
            action()
