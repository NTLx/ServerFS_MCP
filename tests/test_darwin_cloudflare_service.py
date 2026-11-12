from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_service
from serverfs_mcp.cli import build_parser


def _fixture(tmp_path: Path, *, mode: int = 0o600) -> tuple[Path, Path, Path, Path, Path]:
    workdir = tmp_path / "staging"
    workdir.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(
        f'[[workdirs]]\nalias = "stage"\npath = "{workdir}"\nread_only = true\n',
        encoding="utf-8",
    )
    token = tmp_path / "operator" / "connector.token"
    token.parent.mkdir()
    token.write_text("connector-secret", encoding="utf-8")
    token.chmod(mode)
    env = tmp_path / "operator" / "origin.env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=origin-secret\nSERVERFS_HTTP_ALLOWED_HOSTS=stage.example.test\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    executable = tmp_path / "cloudflared"
    executable.write_text("binary-placeholder", encoding="utf-8")
    executable.chmod(0o700)
    return config, workdir, token, env, executable


@pytest.fixture(autouse=True)
def _allow_live_port_for_unit_specs(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.name != "test_loopback_port_availability_checks_real_bind":
        monkeypatch.setattr(darwin_service, "_loopback_port_available", lambda _port: True)


def test_cloudflare_cli_is_explicit_and_legacy_service_cli_is_unchanged() -> None:
    parser = build_parser()
    old = parser.parse_args(["service", "install", "--config", "serverfs.toml"])
    new = parser.parse_args(
        [
            "service",
            "cloudflare",
            "install",
            "--config",
            "serverfs.toml",
            "--cloudflared",
            "/opt/serverfs/cloudflared",
            "--token-file",
            "/private/token",
            "--env-file",
            "/private/http.env",
            "--http-port",
            "8765",
            "--hostname",
            "stage.example.test",
        ]
    )
    assert old.service_command == "install"
    assert new.service_command == "cloudflare"
    assert new.cloudflare_command == "install"
    assert new.http_port == 8765
    serve = parser.parse_args(
        [
            "serve",
            "--config",
            "serverfs.toml",
            "--transport",
            "streamable-http",
            "--http-port",
            "8765",
        ]
    )
    assert serve.port == 8765


def test_cloudflare_service_specs_use_token_file_path_and_secure_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    specs = darwin_service._cloudflare_service_specs(
        config, executable, token, env, 8765, "stage.example.test"
    )
    connector = specs[darwin_service.CLOUDFLARE_SERVICE]
    origin = specs[darwin_service.HTTP_ORIGIN_SERVICE]
    assert connector["arguments"] == [
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token-file",
        str(token),
    ]
    assert "connector-secret" not in json.dumps(specs)
    assert "origin-secret" not in json.dumps(specs)
    snapshot = Path(str(origin["environment_file"]))
    assert snapshot == state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT
    assert not snapshot.exists()
    assert "--http-port" in origin["arguments"]
    assert "127.0.0.1" not in origin["arguments"]
    assert origin["arguments"][origin["arguments"].index("--config") + 1] == str(config.resolve())


def test_cloudflare_service_specs_accept_explicit_writable_workdir_without_changing_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workdir, token, env, executable = _fixture(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace("read_only = true", "read_only = false"),
        encoding="utf-8",
    )
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")

    specs = darwin_service._cloudflare_service_specs(
        config, executable, token, env, 8765, "stage.example.test"
    )

    from serverfs_mcp.native_config import load_native_config

    workdirs, _settings = load_native_config(config)
    assert [(item.alias, item.root, item.read_only) for item in workdirs] == [
        ("stage", workdir, False)
    ]
    origin = specs[darwin_service.HTTP_ORIGIN_SERVICE]
    assert origin["arguments"][origin["arguments"].index("--config") + 1] == str(config.resolve())


def test_cloudflare_service_specs_preserve_read_only_workdir_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workdir, token, env, executable = _fixture(tmp_path)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")

    darwin_service._cloudflare_service_specs(
        config, executable, token, env, 8765, "stage.example.test"
    )

    from serverfs_mcp.native_config import load_native_config

    workdirs, _settings = load_native_config(config)
    assert [(item.alias, item.root, item.read_only) for item in workdirs] == [
        ("stage", workdir, True)
    ]


def test_cloudflare_service_rejects_no_workdirs_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")

    with pytest.raises(darwin_service.DarwinServiceError, match="at least one workdir"):
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )


def test_http_origin_spec_preserves_symlinked_virtualenv_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    real_executable = tmp_path / "python-real"
    real_executable.touch()
    symlink_executable = tmp_path / "venv" / "bin" / "python"
    symlink_executable.parent.mkdir(parents=True)
    symlink_executable.symlink_to(real_executable)
    monkeypatch.setattr(darwin_service.sys, "executable", str(symlink_executable))

    specs = darwin_service._cloudflare_service_specs(
        config, executable, token, env, 8765, "stage.example.test"
    )

    assert specs[darwin_service.HTTP_ORIGIN_SERVICE]["executable"] == str(
        symlink_executable.absolute()
    )


def test_loopback_port_availability_checks_real_bind() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert not darwin_service._loopback_port_available(port)
    assert darwin_service._loopback_port_available(port)


def test_cloudflare_service_rejects_insecure_or_confined_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    config, workdir, token, env, executable = _fixture(tmp_path, mode=0o644)
    with pytest.raises(darwin_service.DarwinServiceError, match="mode 0600"):
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )
    token.chmod(0o600)
    inside = workdir / "token"
    inside.write_text("secret", encoding="utf-8")
    inside.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError, match="outside"):
        darwin_service._cloudflare_service_specs(
            config, executable, inside, env, 8765, "stage.example.test"
        )


@pytest.mark.parametrize("secret_name", ["connector token", "HTTP origin env"])
def test_cloudflare_service_rejects_secrets_inside_second_workdir_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_name: str
) -> None:
    config, first_root, token, env, executable = _fixture(tmp_path)
    second_root = tmp_path / "second-workdir"
    second_root.mkdir()
    config.write_text(
        config.read_text(encoding="utf-8")
        + f'\n[[workdirs]]\nalias = "second"\npath = "{second_root}"\nread_only = false\n',
        encoding="utf-8",
    )
    assert first_root != second_root
    secret = second_root / ("connector.token" if secret_name == "connector token" else "origin.env")
    secret.write_text("private", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")

    with pytest.raises(darwin_service.DarwinServiceError, match="outside"):
        darwin_service._cloudflare_service_specs(
            config,
            executable,
            secret if secret_name == "connector token" else token,
            secret if secret_name == "HTTP origin env" else env,
            8765,
            "stage.example.test",
        )


@pytest.mark.parametrize("secret_name", ["connector token", "HTTP origin env"])
def test_cloudflare_service_rejects_insecure_env_or_token_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_name: str
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    target = token if secret_name == "connector token" else env
    target.chmod(0o644)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")

    with pytest.raises(darwin_service.DarwinServiceError, match="mode 0600"):
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )


def test_http_origin_env_rejects_unrelated_secrets_without_echoing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    config, _workdir, token, env, executable = _fixture(tmp_path)
    env.write_text(
        env.read_text(encoding="utf-8") + "CONTROL_PLANE_API_KEY=unrelated-secret\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError) as error:
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )
    assert "unrelated-secret" not in str(error.value)
    assert "unsupported settings" in str(error.value)


def test_http_origin_env_rejects_combined_api_key_and_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    config, _workdir, token, env, executable = _fixture(tmp_path)
    env.write_text(
        "SERVERFS_HTTP_API_KEY=origin-secret\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=stage.example.test\n"
        "SERVERFS_OAUTH_ENABLED=true\n"
        "SERVERFS_OAUTH_INTROSPECTION_URL=https://auth.example.test/introspect\n"
        "SERVERFS_OAUTH_INTROSPECTION_SECRET=oauth-secret\n",
        encoding="utf-8",
    )
    env.chmod(0o600)

    with pytest.raises(darwin_service.DarwinServiceError, match="authentication configuration"):
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )


def test_cloudflare_manifest_merge_preserves_old_services_and_never_registers_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps({"schema_version": 1, "services": {"tunnel": {"old": True}}}).encode(),
    )
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: tmp_path / "manager")
    (tmp_path / "manager").touch()
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda _app: None)
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    monkeypatch.setattr(darwin_service, "_refuse_unmanaged_cloudflared", lambda: None)
    registered: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_register",
        lambda service: registered.append(service) or {service: "enabled"},
    )
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda _service: 1)
    monkeypatch.setattr(darwin_service, "_wait_http_origin", lambda *_args: None)
    darwin_service.install_cloudflare_service(
        config, executable, token, env, 8765, "stage.example.test"
    )
    document = darwin_service._load_manifest_document()
    assert set(document["services"]) == {"tunnel", "http-origin", "cloudflare"}
    assert registered == ["http-origin", "cloudflare"]
    assert "connector-secret" not in darwin_service.manifest_path().read_text()
    snapshot = Path(document["services"]["http-origin"]["environment_file"])
    assert json.loads(snapshot.read_text(encoding="utf-8")) == {
        "SERVERFS_HTTP_API_KEY": "origin-secret",
        "SERVERFS_HTTP_ALLOWED_HOSTS": "stage.example.test",
    }
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_cloudflare_install_revalidates_second_env_read_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: tmp_path / "manager")
    (tmp_path / "manager").touch()
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda _app: None)
    monkeypatch.setattr(
        darwin_service, "native_status", lambda service: {service: "not-registered"}
    )
    monkeypatch.setattr(darwin_service, "_refuse_unmanaged_cloudflared", lambda: None)
    real_read = darwin_service._read_env_file
    reads = 0

    def changed_on_second_read(path: Path) -> dict[str, str]:
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_read(path)
        return {
            "SERVERFS_HTTP_API_KEY": "changed-secret",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "*.example.test",
        }

    monkeypatch.setattr(darwin_service, "_read_env_file", changed_on_second_read)
    with pytest.raises(darwin_service.DarwinServiceError, match="exactly match public hostname"):
        darwin_service.install_cloudflare_service(
            config, executable, token, env, 8765, "stage.example.test"
        )
    assert not (state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


@pytest.mark.parametrize(
    "allowed_hosts",
    ["stage.example.test:8765", "*.example.test", "stage.example.test,other.example.test"],
)
def test_cloudflare_requires_exact_public_hostname_without_port_or_wildcard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed_hosts: str
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    config, _workdir, token, env, executable = _fixture(tmp_path)
    env.write_text(
        f"SERVERFS_HTTP_API_KEY=origin-secret\nSERVERFS_HTTP_ALLOWED_HOSTS={allowed_hosts}\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    with pytest.raises(darwin_service.DarwinServiceError, match="exactly match public hostname"):
        darwin_service._cloudflare_service_specs(
            config, executable, token, env, 8765, "stage.example.test"
        )


def test_legacy_manifest_update_preserves_opt_in_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    preserved = {"executable": "/cloudflared", "arguments": ["run"]}
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps({"schema_version": 1, "services": {"cloudflare": preserved}}).encode(),
    )
    monkeypatch.setattr(darwin_service, "_tunnel_arguments", lambda *_args: ["tunnel"])
    monkeypatch.setattr(darwin_service, "service_log_dir", lambda: tmp_path / "logs")
    darwin_service._write_manifest(tmp_path / "serverfs.toml", {}, bridge_enabled=False)
    document = darwin_service._load_manifest_document()
    assert document["services"]["cloudflare"] == preserved
    assert set(document["services"]) == {"tunnel", "cloudflare"}


def test_legacy_start_ignores_opt_in_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "tunnel": {},
                    "agent-bridge": {},
                    "http-origin": {},
                    "cloudflare": {},
                },
            }
        ).encode(),
    )
    started: list[str] = []
    monkeypatch.setattr(
        darwin_service,
        "_register_and_wait",
        lambda service: started.append(service) or {service: "enabled"},
    )
    darwin_service.start()
    assert started == ["tunnel", "agent-bridge"]


def test_cloudflare_failed_second_registration_rolls_back_only_opt_in_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    monkeypatch.setattr(darwin_service, "service_home", lambda: tmp_path / "state")
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps({"schema_version": 1, "services": {"tunnel": {"legacy": True}}}).encode(),
    )
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: tmp_path / "manager")
    (tmp_path / "manager").touch()
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda _app: None)
    states = {"http-origin": "not-registered", "cloudflare": "not-registered"}
    monkeypatch.setattr(darwin_service, "native_status", lambda service: {service: states[service]})
    monkeypatch.setattr(darwin_service, "_refuse_unmanaged_cloudflared", lambda: None)
    monkeypatch.setattr(
        darwin_service,
        "_cloudflare_service_specs",
        lambda *_args: {service: {"executable": "/x"} for service in states},
    )

    def register(service: str) -> dict[str, str]:
        states[service] = "enabled" if service == "http-origin" else "requires-approval"
        return {service: states[service]}

    removed: list[str] = []
    monkeypatch.setattr(darwin_service, "_register", register)
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda _service: 42)
    monkeypatch.setattr(darwin_service, "_wait_http_origin", lambda *_args: None)

    def unregister(service: str) -> dict[str, str]:
        removed.append(service)
        states[service] = "not-registered"
        return {service: states[service]}

    monkeypatch.setattr(darwin_service, "_unregister_and_wait", unregister)
    with pytest.raises(darwin_service.DarwinServiceError, match="did not become enabled"):
        darwin_service.install_cloudflare_service(
            config, executable, token, env, 8765, "stage.example.test"
        )
    assert removed == ["cloudflare", "http-origin"]
    assert darwin_service._load_manifest_document()["services"] == {"tunnel": {"legacy": True}}
    assert not (tmp_path / "state" / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT).exists()


@pytest.mark.parametrize("failure", ["unregister", "status"])
def test_cloudflare_failed_rollback_preserves_recovery_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config, _workdir, token, env, executable = _fixture(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state_dir)
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps({"schema_version": 1, "services": {"tunnel": {"legacy": True}}}).encode(),
    )
    monkeypatch.setattr(darwin_service, "manager_executable", lambda: tmp_path / "manager")
    (tmp_path / "manager").touch()
    monkeypatch.setattr(darwin_service, "_verify_app", lambda _app: None)
    monkeypatch.setattr(darwin_service, "_verify_opt_in_agents", lambda _app: None)
    states = {"http-origin": "not-registered", "cloudflare": "not-registered"}
    status_calls = {service: 0 for service in states}

    def native_status(service: str) -> dict[str, str]:
        status_calls[service] += 1
        if failure == "status" and service == "cloudflare" and status_calls[service] > 1:
            raise darwin_service.DarwinServiceError("private status detail")
        return {service: states[service]}

    monkeypatch.setattr(darwin_service, "native_status", native_status)
    monkeypatch.setattr(darwin_service, "_refuse_unmanaged_cloudflared", lambda: None)
    monkeypatch.setattr(
        darwin_service,
        "_cloudflare_service_specs",
        lambda *_args: {service: {"executable": "/x"} for service in states},
    )

    def register(service: str) -> dict[str, str]:
        states[service] = "enabled" if service == "http-origin" else "requires-approval"
        return {service: states[service]}

    monkeypatch.setattr(darwin_service, "_register", register)
    monkeypatch.setattr(darwin_service, "_wait_managed_job", lambda _service: 42)
    monkeypatch.setattr(darwin_service, "_wait_http_origin", lambda *_args: None)

    def unregister(service: str) -> dict[str, str]:
        if failure == "unregister" and service == "cloudflare":
            raise darwin_service.DarwinServiceError("private unregister detail")
        states[service] = "not-registered"
        return {service: states[service]}

    monkeypatch.setattr(darwin_service, "_unregister_and_wait", unregister)
    with pytest.raises(
        darwin_service.DarwinServiceError,
        match="rollback incomplete; preserved for recovery",
    ) as error:
        darwin_service.install_cloudflare_service(
            config, executable, token, env, 8765, "stage.example.test"
        )
    assert "private" not in str(error.value)
    services = darwin_service._load_manifest_document()["services"]
    assert set(services) == {"tunnel", "http-origin", "cloudflare"}
    assert services["tunnel"] == {"legacy": True}
    snapshot = state_dir / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT
    assert snapshot.exists()
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_cloudflare_uninstall_removes_only_derived_environment_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    snapshot = state / darwin_service._HTTP_ORIGIN_ENV_SNAPSHOT
    darwin_service._atomic_private_write(snapshot, b'{"SERVERFS_HTTP_API_KEY":"secret"}')
    preserved = state / "openai-private.json"
    darwin_service._atomic_private_write(preserved, b'{"private":true}')
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "http-origin": {},
                    "cloudflare": {},
                    "tunnel": {"keep": True},
                },
            }
        ).encode(),
    )
    monkeypatch.setattr(darwin_service, "cloudflare_stop", lambda: None)
    darwin_service.cloudflare_uninstall()
    assert not snapshot.exists()
    assert preserved.read_bytes() == b'{"private":true}'
    assert darwin_service._load_manifest_document()["services"] == {"tunnel": {"keep": True}}


@pytest.mark.skipif(
    sys.platform == "darwin", reason="non-Darwin CLI rejection is platform-specific"
)
def test_cloudflare_cli_refuses_non_darwin_without_lifecycle_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serverfs_mcp import cli

    args = build_parser().parse_args(["service", "cloudflare", "status"])
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli.cmd_service(args) == 2


def test_cloudflare_restart_waits_for_lingering_connector_to_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    token = tmp_path / "operator" / "connector.token"
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "http-origin": {"arguments": ["serve"]},
                    "cloudflare": {
                        "arguments": [
                            "tunnel",
                            "--no-autoupdate",
                            "run",
                            "--token-file",
                            str(token),
                        ]
                    },
                },
            }
        ).encode(),
    )

    class Completed:
        returncode = 0

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    polls = 0

    def stub_run(*_args: object, **_kwargs: object) -> Completed:
        nonlocal polls
        polls += 1
        output = (
            f"9001 cloudflared tunnel --no-autoupdate run --token-file {token}"
            if polls <= 2
            else ""
        )
        return Completed(output)

    monkeypatch.setattr(
        darwin_service,
        "subprocess",
        type(
            "FakeSubprocess",
            (),
            {"run": staticmethod(stub_run), "SubprocessError": subprocess.SubprocessError},
        ),
    )
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _service: None)
    monkeypatch.setattr(darwin_service, "cloudflare_stop", lambda: {})
    started: list[bool] = []

    def start() -> dict[str, str]:
        darwin_service._refuse_unmanaged_cloudflared()
        started.append(True)
        return {"started": "yes"}

    monkeypatch.setattr(darwin_service, "cloudflare_start", start)
    clock = [0.0]
    monkeypatch.setattr(darwin_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        darwin_service.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    assert darwin_service.cloudflare_restart() == {"started": "yes"}
    assert polls == 4
    assert started == [True]


def test_cloudflare_restart_refuses_foreign_process_with_manifest_token_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(darwin_service, "service_home", lambda: state)
    token = tmp_path / "operator" / "connector.token"
    darwin_service._atomic_private_write(
        darwin_service.manifest_path(),
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "http-origin": {"arguments": ["serve"]},
                    "cloudflare": {"arguments": ["tunnel", "run", "--token-file", str(token)]},
                },
            }
        ).encode(),
    )

    class Completed:
        returncode = 0

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    polls = 0

    def stub_run(*_args: object, **_kwargs: object) -> Completed:
        nonlocal polls
        polls += 1
        # The foreign connector deliberately carries the exact manifest token path.
        running = True
        output = (
            f"9002 cloudflared tunnel --no-autoupdate run --token-file {token}" if running else ""
        )
        return Completed(output)

    monkeypatch.setattr(
        darwin_service,
        "subprocess",
        type(
            "FakeSubprocess",
            (),
            {"run": staticmethod(stub_run), "SubprocessError": subprocess.SubprocessError},
        ),
    )
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _service: None)
    monkeypatch.setattr(darwin_service, "cloudflare_stop", lambda: {})
    started: list[bool] = []
    monkeypatch.setattr(darwin_service, "cloudflare_start", lambda: started.append(True) or {})
    monkeypatch.setattr(darwin_service, "_CLOUDFLARE_RESTART_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(darwin_service, "_CLOUDFLARE_RESTART_POLL_SECONDS", 0.25)
    clock = [0.0]
    monkeypatch.setattr(darwin_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        darwin_service.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    with pytest.raises(
        darwin_service.DarwinServiceError, match="unmanaged cloudflared tunnel process"
    ):
        darwin_service.cloudflare_restart()
    assert started == []
    assert polls >= 2


def test_refuse_unmanaged_cloudflared_rejects_token_path_without_managed_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "connector.token"
    monkeypatch.setattr(darwin_service, "_managed_job_pid", lambda _service: None)

    class Completed:
        returncode = 0
        stdout = f"9001 cloudflared tunnel run --token-file {token}"

    monkeypatch.setattr(
        darwin_service,
        "subprocess",
        type(
            "FakeSubprocess",
            (),
            {
                "run": staticmethod(lambda *_args, **_kwargs: Completed()),
                "SubprocessError": subprocess.SubprocessError,
            },
        ),
    )
    with pytest.raises(
        darwin_service.DarwinServiceError, match="unmanaged cloudflared tunnel process"
    ):
        darwin_service._refuse_unmanaged_cloudflared()
