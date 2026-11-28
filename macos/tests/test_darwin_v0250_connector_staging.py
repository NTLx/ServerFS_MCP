"""macOS v0.25.0 independent Cloudflare connector staging regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import cli, darwin_service


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "workdir"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(
        f'[[workdirs]]\nalias = "repo"\npath = "{root}"\nread_only = false\n',
        encoding="utf-8",
    )
    secrets = tmp_path / "private"
    secrets.mkdir(mode=0o700)
    env = secrets / "http.env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=private-test-key\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=stage.example.test,127.0.0.1:18766\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    token = secrets / "token"
    token.write_text("private-connector-token", encoding="utf-8")
    token.chmod(0o600)
    cloudflared = tmp_path / "cloudflared"
    cloudflared.write_text("test-binary", encoding="utf-8")
    cloudflared.chmod(0o700)
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    monkeypatch.setattr(darwin_service, "_loopback_port_available", lambda _port: True)
    manager = tmp_path / "service-manager"
    manager.touch()
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: manager)
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_refuse_unmanaged_cloudflared", lambda: None)
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _svc: None)
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    darwin_service.stage_core_service(config, env, 18766, "stage.example.test")
    return config, token, cloudflared, state


def test_connector_staging_is_manifest_only_and_keeps_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, state = _fixture(tmp_path, monkeypatch)
    original_core = darwin_service._load_manifest_document()["services"]["http-origin"]
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda svc: pytest.fail(f"must not register {svc}"),
    )
    assert darwin_service.stage_cloudflare_connector(
        config, executable, token, "stage.example.test"
    ) == {"cloudflare": "staged-not-registered"}
    document = darwin_service._load_manifest_document()
    assert document["services"]["http-origin"] == original_core
    connector = document["services"]["cloudflare"]
    assert connector["connector_owner"] == "independent-connector"
    assert connector["core_port"] == 18766
    assert connector["arguments"][-2:] == ["--token-file", str(token)]
    assert connector["working_directory"] == str(config.parent)
    assert "private-connector-token" not in json.dumps(document)
    assert "private-test-key" not in json.dumps(document)
    assert darwin_service.manifest_path().stat().st_mode & 0o777 == 0o600
    assert (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).is_file()


def test_unstage_connector_preserves_core_and_source_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    snapshot = state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT
    before = snapshot.read_bytes()
    darwin_service.unstage_cloudflare_connector()
    services = darwin_service._load_manifest_document()["services"]
    assert set(services) == {"http-origin"}
    assert services["http-origin"]["origin_owner"] == "independent-core"
    assert snapshot.read_bytes() == before
    assert token.read_text(encoding="utf-8") == "private-connector-token"


@pytest.mark.parametrize("registration", ["enabled", "requires-approval"])
def test_unstage_refuses_registered_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registration: str
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: registration})
    with pytest.raises(darwin_service.DarwinServiceError, match="remains registered"):
        darwin_service.unstage_cloudflare_connector()
    assert "cloudflare" in darwin_service._load_manifest_document()["services"]


def test_unstage_refuses_live_process_despite_unregistered_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda service: 31337)
    with pytest.raises(darwin_service.DarwinServiceError, match="still running"):
        darwin_service.unstage_cloudflare_connector()


def test_stage_connector_refuses_mismatched_hostname_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    initial = darwin_service.manifest_path().read_bytes()
    with pytest.raises(darwin_service.DarwinServiceError, match="exactly match"):
        darwin_service.stage_cloudflare_connector(config, executable, token, "wrong.example.test")
    other = tmp_path / "other.toml"
    other.write_bytes(config.read_bytes())
    with pytest.raises(darwin_service.DarwinServiceError, match="canonical TOML"):
        darwin_service.stage_cloudflare_connector(other, executable, token, "stage.example.test")
    assert darwin_service.manifest_path().read_bytes() == initial


def test_stage_connector_refuses_exposed_or_insecure_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    token.chmod(0o644)
    with pytest.raises(darwin_service.DarwinServiceError, match="mode 0600"):
        darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    token.chmod(0o600)
    exposed = tmp_path / "workdir" / "token"
    exposed.write_bytes(token.read_bytes())
    exposed.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError, match="outside"):
        darwin_service.stage_cloudflare_connector(config, executable, exposed, "stage.example.test")
    assert "cloudflare" not in darwin_service._load_manifest_document()["services"]


def test_stage_connector_refuses_legacy_origin_and_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    document = darwin_service._load_manifest_document()
    document["services"]["http-origin"].pop("origin_owner")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(), json.dumps(document).encode()
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="independent Core"):
        darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    document["services"]["http-origin"]["origin_owner"] = "independent-core"
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(), json.dumps(document).encode()
    )
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    with pytest.raises(darwin_service.DarwinServiceError, match="already exists"):
        darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")


def test_legacy_pair_stop_cannot_unregister_independent_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    monkeypatch.setattr(
        darwin_service,
        "_unregister_and_wait",
        lambda service: pytest.fail(f"old pair must not stop {service}"),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="independent Core"):
        darwin_service.cloudflare_stop()


def test_unstage_refuses_legacy_connector_without_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, token, executable, _state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_cloudflare_connector(config, executable, token, "stage.example.test")
    document = darwin_service._load_manifest_document()
    document["services"]["cloudflare"].pop("connector_owner")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(), json.dumps(document).encode()
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="no independently staged"):
        darwin_service.unstage_cloudflare_connector()


def test_cli_stage_dispatch_and_windows_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "service",
            "cloudflare",
            "stage",
            "--config",
            "/private/serverfs.toml",
            "--cloudflared",
            "/private/cloudflared",
            "--token-file",
            "/private/token",
            "--hostname",
            "stage.example.test",
        ]
    )
    assert args.cloudflare_command == "stage"
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    called: list[tuple[Path, Path, Path, str]] = []
    monkeypatch.setattr(
        darwin_service,
        "stage_cloudflare_connector",
        lambda *values: called.append(values) or {"cloudflare": "staged-not-registered"},
    )
    assert cli.cmd_service(args) == 0
    assert called == [
        (
            Path("/private/serverfs.toml"),
            Path("/private/cloudflared"),
            Path("/private/token"),
            "stage.example.test",
        )
    ]
    assert "staged-not-registered" in capsys.readouterr().err
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.cmd_service(args) == 2
    assert "requires macOS" in capsys.readouterr().err


def test_cli_connector_unstage_does_not_touch_core(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(["service", "cloudflare", "unstage"])
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    calls: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "unstage_cloudflare_connector",
        lambda: calls.append("unstage-cloudflare"),
    )
    assert cli.cmd_service(args) == 0
    assert calls == ["unstage-cloudflare"]
    assert "staging removed" in capsys.readouterr().err
