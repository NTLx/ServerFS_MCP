"""v0.25.0 macOS independent Core staging security and no-live-mutation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import cli, darwin_service


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(
        f'[[workdirs]]\nalias = "repo"\npath = "{workdir}"\nread_only = false\n',
        encoding="utf-8",
    )
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    env = private / "core.env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=private-test-key\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=example.test,127.0.0.1:18766\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    monkeypatch.setattr(darwin_service, "_loopback_port_available", lambda port: True)
    manager = tmp_path / "manager"
    manager.touch()
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: manager)
    monkeypatch.setattr(darwin_service, "_verify_app", lambda app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda app: None)
    monkeypatch.setattr(
        darwin_service,
        "native_status",
        lambda service: {service: "not-registered"},
    )
    return config, env, state


def test_stage_core_keeps_legacy_manifest_and_never_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {"schema_version": 1, "services": {
                "tunnel": {"legacy": True, "arguments": ["--config", str(config)]}
            }}
        ).encode(),
    )
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: pytest.fail(f"stage must not register {service}"),
    )
    assert darwin_service.stage_core_service(config, env, 18766, "example.test") == {
        "http-origin": "staged-not-registered"
    }
    manifest = darwin_service._load_manifest_document()
    assert manifest["services"]["tunnel"] == {
        "legacy": True,
        "arguments": ["--config", str(config)],
    }
    spec = manifest["services"]["http-origin"]
    assert spec["origin_owner"] == "independent-core"
    assert spec["arguments"][spec["arguments"].index("--config") + 1] == str(config.resolve())
    assert spec["arguments"][spec["arguments"].index("--http-port") + 1] == "18766"
    assert "private-test-key" not in json.dumps(manifest)
    snapshot = state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT
    assert json.loads(snapshot.read_text(encoding="utf-8")) == {
        "SERVERFS_HTTP_API_KEY": "private-test-key",
        "SERVERFS_HTTP_ALLOWED_HOSTS": "example.test,127.0.0.1:18766",
    }
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert darwin_service.manifest_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "raw",
    [
        "example.test",
        "example.test,127.0.0.1:*",
        "127.0.0.1:18766,example.test",
        "example.test,localhost:18766",
        "example.test,127.0.0.1:18766,evil.test",
    ],
)
def test_stage_core_requires_exact_dual_ingress_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    env.write_text(
        f"SERVERFS_HTTP_API_KEY=test-secret\nSERVERFS_HTTP_ALLOWED_HOSTS={raw}\n",
        encoding="utf-8",
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="exactly match"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert not darwin_service.manifest_path().exists()
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


def test_stage_core_refuses_live_or_already_staged_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: "enabled"})
    with pytest.raises(darwin_service.DarwinServiceError, match="already registered"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    darwin_service.stage_core_service(config, env, 18766, "example.test")
    with pytest.raises(darwin_service.DarwinServiceError, match="already exists"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")


def test_stage_core_refuses_private_env_inside_exposed_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    exposed_env = tmp_path / "repo" / "core.env"
    exposed_env.write_bytes(env.read_bytes())
    exposed_env.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError, match="outside"):
        darwin_service.stage_core_service(config, exposed_env, 18766, "example.test")
    assert not darwin_service.manifest_path().exists()
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


def test_stage_core_refuses_unknown_or_conflicting_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    env.write_text(
        "SERVERFS_HTTP_API_KEY=secret\nSERVERFS_HTTP_ALLOWED_HOSTS="
        "example.test,127.0.0.1:18766\nUNRELATED_SECRET=oops\n",
        encoding="utf-8",
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="unsupported settings"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


def test_stage_core_manifest_publish_failure_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    original = json.dumps(
        {"schema_version": 1, "services": {
            "tunnel": {"legacy": True, "arguments": ["--config", str(config)]}
        }}
    )
    darwin_service._atomic_private_write(darwin_service.manifest_path(), original.encode())
    writer = darwin_service._atomic_private_write

    def fail_manifest(path: Path, payload: bytes) -> None:
        if path == darwin_service.manifest_path():
            raise OSError("synthetic manifest write failure")
        writer(path, payload)

    monkeypatch.setattr(darwin_service, "_atomic_private_write", fail_manifest)
    with pytest.raises(OSError, match="synthetic"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert darwin_service.manifest_path().read_text(encoding="utf-8") == original
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


def test_unstage_requires_own_core_unregistered_and_preserves_other_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    darwin_service.stage_core_service(config, env, 18766, "example.test")
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: "enabled"})
    with pytest.raises(darwin_service.DarwinServiceError, match="still registered"):
        darwin_service.unstage_core_service()
    assert darwin_service.manifest_path().exists()
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    darwin_service.unstage_core_service()
    assert "http-origin" not in darwin_service._load_manifest_document()["services"]
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


def test_unstage_refuses_legacy_or_connector_owned_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {
                "schema_version": 1,
                "services": {"http-origin": {"arguments": ["legacy"]}},
            }
        ).encode(),
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="no independently staged"):
        darwin_service.unstage_core_service()
    assert "http-origin" in darwin_service._load_manifest_document()["services"]
    darwin_service.manifest_path().unlink()
    darwin_service.stage_core_service(config, env, 18766, "example.test")
    manifest = darwin_service._load_manifest_document()
    manifest["services"]["cloudflare"] = {"legacy": True}
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(), json.dumps(manifest).encode()
    )
    with pytest.raises(darwin_service.DarwinServiceError, match="Cloudflare connector"):
        darwin_service.unstage_core_service()
    assert "http-origin" in darwin_service._load_manifest_document()["services"]


def test_core_cli_stage_and_status_are_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = cli.build_parser()
    stage = parser.parse_args(
        [
            "service",
            "core",
            "stage",
            "--config",
            "/tmp/config.toml",
            "--env-file",
            "/tmp/core.env",
            "--http-port",
            "18766",
            "--hostname",
            "example.test",
        ]
    )
    assert stage.core_command == "stage"
    assert stage.http_port == 18766
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    called: list[tuple[Path, Path, int, str]] = []
    monkeypatch.setattr(
        darwin_service,
        "stage_core_service",
        lambda config, env, port, host: (
            called.append((config, env, port, host)) or {"http-origin": "staged-not-registered"}
        ),
    )
    assert cli.cmd_service(stage) == 0
    assert called == [(Path("/tmp/config.toml"), Path("/tmp/core.env"), 18766, "example.test")]
    assert "staged-not-registered" in capsys.readouterr().err
    monkeypatch.setattr(
        darwin_service,
        "core_status",
        lambda: {"registration": "not-registered", "independent_core": True},
    )
    assert cli.cmd_service(parser.parse_args(["service", "core", "status"])) == 0
    assert '"independent_core": true' in capsys.readouterr().err


def test_core_cli_fails_closed_on_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli.cmd_service(cli.build_parser().parse_args(["service", "core", "status"])) == 2
    assert "requires macOS" in capsys.readouterr().err


def test_stage_core_refuses_existing_connector_manifest_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    original = json.dumps(
        {"schema_version": 1, "services": {"cloudflare": {"legacy": True}}}
    )
    darwin_service._atomic_private_write(darwin_service.manifest_path(), original.encode())
    with pytest.raises(darwin_service.DarwinServiceError, match="migration requires review"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert darwin_service.manifest_path().read_text(encoding="utf-8") == original
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


@pytest.mark.parametrize("tunnel_args", [
    ["--config", "/private/other.toml"],
    ["--tunnel-id", "example"],
    ["--config", "relative.toml"],
])
def test_stage_core_requires_same_canonical_toml_as_installed_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tunnel_args: list[str]
) -> None:
    config, env, state = _fixture(tmp_path, monkeypatch)
    original = json.dumps(
        {"schema_version": 1, "services": {
            "tunnel": {"arguments": tunnel_args},
        }}
    )
    darwin_service._atomic_private_write(darwin_service.manifest_path(), original.encode())
    with pytest.raises(darwin_service.DarwinServiceError, match="Core"):
        darwin_service.stage_core_service(config, env, 18766, "example.test")
    assert darwin_service.manifest_path().read_text(encoding="utf-8") == original
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()
