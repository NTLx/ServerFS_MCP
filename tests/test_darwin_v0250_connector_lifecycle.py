"""v0.25.0 macOS connector-only lifecycle regression (source-only, no live jobs)."""

from __future__ import annotations

import argparse

import pytest

from serverfs_mcp import cli, darwin_service


def test_parser_exposes_explicit_connector_only_operations() -> None:
    parser = cli.build_parser()
    for operation in ("start", "stop", "restart"):
        for component in ("tunnel", "cloudflare", "core"):
            args = parser.parse_args(["service", operation, "--component", component])
            assert args.service_command == operation
            assert args.component == component
        assert parser.parse_args(["service", operation]).component is None


def test_cloudflare_connector_start_only_registers_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_load_manifest_document",
        lambda: {"services": {"http-origin": {}, "cloudflare": {}}},
    )
    monkeypatch.setattr(
        darwin_service,
        "_refuse_unmanaged_cloudflared",
        lambda: events.append("check-cloudflared"),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: events.append(f"register:{service}") or {service: "enabled"},
    )
    assert darwin_service.cloudflare_connector_start() == {"cloudflare": "enabled"}
    assert events == ["check-cloudflared", "register:cloudflare"]


def test_cloudflare_connector_stop_does_not_unregister_http_origin_or_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_load_manifest_document",
        lambda: {"services": {"http-origin": {}, "cloudflare": {}, "agent-bridge": {}}},
    )
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: events.append(service) or {service: "not-registered"},
    )
    assert darwin_service.cloudflare_connector_stop() == {"cloudflare": "not-registered"}
    assert events == ["cloudflare"]


def test_cloudflare_connector_restart_is_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "cloudflare_connector_stop",
        lambda: events.append("stop:cloudflare") or {"cloudflare": "not-registered"},
    )
    monkeypatch.setattr(
        darwin_service,
        "cloudflare_connector_start",
        lambda: events.append("start:cloudflare") or {"cloudflare": "enabled"},
    )
    assert darwin_service.cloudflare_connector_restart() == {"cloudflare": "enabled"}
    assert events == ["stop:cloudflare", "start:cloudflare"]


@pytest.mark.parametrize("operation", ["cloudflare_connector_start", "cloudflare_connector_stop"])
def test_cloudflare_connector_operations_refuse_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(darwin_service, "_load_manifest_document", lambda: {"services": {}})
    with pytest.raises(darwin_service.DarwinServiceError, match="not installed"):
        getattr(darwin_service, operation)()


@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
@pytest.mark.parametrize("component", ["tunnel", "cloudflare", "core"])
def test_cli_explicit_component_never_invokes_group_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    component: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    for group_operation in ("start", "stop", "restart"):
        monkeypatch.setattr(
            darwin_service,
            group_operation,
            lambda: pytest.fail("grouped lifecycle must not run"),
        )
    seen: list[tuple[str, str]] = []
    if component == "tunnel":
        method = {
            "start": "register_service",
            "stop": "unregister_service",
            "restart": "restart_service",
        }[operation]
        monkeypatch.setattr(
            darwin_service,
            method,
            lambda service: seen.append((operation, service)) or {service: "enabled"},
        )
    else:
        method = (
            f"cloudflare_connector_{operation}" if component == "cloudflare" else f"core_{operation}"
        )
        service_id = "cloudflare" if component == "cloudflare" else "http-origin"
        monkeypatch.setattr(
            darwin_service,
            method,
            lambda: seen.append((operation, component)) or {service_id: "enabled"},
        )
    args = cli.build_parser().parse_args(["service", operation, "--component", component])
    assert cli.cmd_service(args) == 0
    assert seen == [(operation, component)]
    rendered = "http-origin" if component == "core" else component
    assert f"{rendered}: enabled" in capsys.readouterr().err


def test_windows_component_option_fails_closed_without_touching_services(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "win32")
    args: argparse.Namespace = cli.build_parser().parse_args(
        ["service", "stop", "--component", "tunnel"]
    )
    assert cli.cmd_service(args) == 2
    assert "not available on Windows" in capsys.readouterr().err
