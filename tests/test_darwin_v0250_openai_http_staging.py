"""macOS v0.25.0 OpenAI Tunnel direct HTTP-to-Core staging regression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import cli, darwin_service
from test_darwin_v0250_connector_staging import _fixture


def _openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    config, cloud_token, cloudflared, state = _fixture(tmp_path, monkeypatch)
    private = tmp_path / "private"
    cp_key = private / "control.key"
    cp_key.write_text("private-control-key", encoding="utf-8")
    cp_key.chmod(0o600)
    bearer = private / "core-bearer"
    bearer.write_text("Bearer private-test-key", encoding="utf-8")
    bearer.chmod(0o600)
    client = tmp_path / "tunnel-client"
    client.write_text("test-client", encoding="utf-8")
    client.chmod(0o700)
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_pids", lambda: [])
    return config, cp_key, bearer, client, cloud_token, cloudflared, state


TUNNEL_ID = "tunnel_" + "a" * 32


def test_http_ingress_staging_has_no_stdio_child_or_secret_in_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, state = _openai(tmp_path, monkeypatch)
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: pytest.fail(f"staging must not register {service}"),
    )
    result = darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    assert result == {"tunnel": "staged-not-registered"}
    services = darwin_service._load_manifest_document()["services"]
    assert set(services) == {"http-origin", "tunnel"}
    spec = services["tunnel"]
    args = spec["arguments"]
    assert spec["connector_owner"] == "independent-openai-http"
    assert spec["executable"] == str(client)
    assert spec["core_port"] == 18766
    assert args[args.index("--mcp.server-url") + 1] == "url=http://127.0.0.1:18766/mcp"
    assert args[args.index("--control-plane.api-key") + 1] == f"file:{cp_key}"
    assert args[args.index("--mcp.extra-headers") + 1] == f"Authorization: file:{bearer}"
    assert args[args.index("--mcp.discovery-extra-headers") + 1] == (
        f"Authorization: file:{bearer}"
    )
    assert "--mcp.command" not in args
    assert args[args.index("--health.listen-addr") + 1] == "127.0.0.1:0"
    assert "supervisor" not in json.dumps(services)
    assert "private-test-key" not in json.dumps(services)
    assert "private-control-key" not in json.dumps(services)
    assert darwin_service.manifest_path().stat().st_mode & 0o777 == 0o600
    assert (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).is_file()


def test_http_ingress_and_cloudflare_share_one_core_and_unstage_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, token, cloudflared, state = _openai(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, cloudflared, token, "stage.example.test")
    original_core = darwin_service._load_manifest_document()["services"]["http-origin"]
    darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    services = darwin_service._load_manifest_document()["services"]
    assert set(services) == {"http-origin", "cloudflare", "tunnel"}
    assert services["http-origin"] == original_core
    darwin_service.unstage_openai_http_connector()
    assert set(darwin_service._load_manifest_document()["services"]) == {
        "http-origin",
        "cloudflare",
    }
    assert cp_key.read_text(encoding="utf-8") == "private-control-key"
    assert bearer.read_text(encoding="utf-8") == "Bearer private-test-key"
    assert (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).is_file()


def test_http_ingress_requires_same_core_bearer_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    original = darwin_service.manifest_path().read_bytes()
    bearer.write_text("Bearer incorrect-key", encoding="utf-8")
    with pytest.raises(darwin_service.DarwinServiceError, match="credential file validation"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    assert darwin_service.manifest_path().read_bytes() == original
    bearer.write_text("Bearer private-test-key\n", encoding="utf-8")
    with pytest.raises(darwin_service.DarwinServiceError, match="credential file validation"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)


def test_http_ingress_refuses_exposed_or_insecure_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    cp_key.chmod(0o644)
    with pytest.raises(darwin_service.DarwinServiceError, match="mode 0600"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    cp_key.chmod(0o600)
    exposed = tmp_path / "workdir" / "auth"
    exposed.write_text("private-control-key", encoding="utf-8")
    exposed.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError, match="outside"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, exposed, bearer)
    assert "tunnel" not in darwin_service._load_manifest_document()["services"]


def test_http_ingress_refuses_duplicate_or_live_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: "enabled"})
    with pytest.raises(darwin_service.DarwinServiceError, match="still registered"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    with pytest.raises(darwin_service.DarwinServiceError, match="already exists"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)


def test_http_ingress_refuses_unmanaged_tunnel_and_bad_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    with pytest.raises(darwin_service.DarwinServiceError, match="invalid OpenAI Tunnel ID"):
        darwin_service.stage_openai_http_connector(config, client, "tunnel_invalid", cp_key, bearer)
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_pids", lambda: [3333])
    with pytest.raises(darwin_service.DarwinServiceError, match="unmanaged"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)


def test_http_ingress_unstage_rejects_registered_and_legacy_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: "enabled"})
    with pytest.raises(darwin_service.DarwinServiceError, match="remains registered"):
        darwin_service.unstage_openai_http_connector()
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    document = darwin_service._load_manifest_document()
    document["services"]["tunnel"].pop("connector_owner")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(), json.dumps(document).encode()
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="no independently staged"):
        darwin_service.unstage_openai_http_connector()


def test_core_unstage_refuses_while_openai_connector_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    with pytest.raises(darwin_service.DarwinServiceError, match="OpenAI connector"):
        darwin_service.unstage_core_service()


def test_http_ingress_cli_is_mac_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = cli.build_parser()
    stage = parser.parse_args(
        [
            "service",
            "openai",
            "stage",
            "--config",
            "/private/serverfs.toml",
            "--tunnel-client",
            "/private/tunnel-client",
            "--tunnel-id",
            TUNNEL_ID,
            "--control-plane-key-file",
            "/private/control-key",
            "--core-bearer-file",
            "/private/core-auth",
        ]
    )
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    called: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        darwin_service,
        "stage_openai_http_connector",
        lambda *args: called.append(args) or {"tunnel": "staged-not-registered"},
    )
    assert cli.cmd_service(stage) == 0
    assert len(called) == 1
    assert "staged-not-registered" in capsys.readouterr().err
    unstage = parser.parse_args(["service", "openai", "unstage"])
    monkeypatch.setattr(
        darwin_service,
        "unstage_openai_http_connector",
        lambda: called.append(("unstage",)),
    )
    assert cli.cmd_service(unstage) == 0
    assert called[-1] == ("unstage",)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.cmd_service(stage) == 2
    assert "requires macOS" in capsys.readouterr().err


def test_openai_registration_refuses_unmanaged_tunnel_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    monkeypatch.setattr(darwin_service, "find_unmanaged_tunnel_pids", lambda: [4321])
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: pytest.fail(f"must not register {service}"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="unmanaged"):
        darwin_service.register_service("tunnel")


def test_openai_control_plane_key_must_differ_from_core_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, cp_key, bearer, client, _token, _cloudflared, _state = _openai(tmp_path, monkeypatch)
    cp_key.write_text("private-test-key", encoding="utf-8")
    with pytest.raises(darwin_service.DarwinServiceError, match="credential file validation"):
        darwin_service.stage_openai_http_connector(config, client, TUNNEL_ID, cp_key, bearer)
    assert "tunnel" not in darwin_service._load_manifest_document()["services"]
