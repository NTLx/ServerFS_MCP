"""Isolated Windows Core lifecycle tests: no live Core, Bridge or Connector.

All real OS credential, Task Scheduler and network accesses are substituted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from serverfs_mcp import windows_core_service as core

SID = "S-1-5-21-100-200-300-1000"


@pytest.fixture
def spec(tmp_path: Path) -> core.CoreRuntimeSpec:
    return core.CoreRuntimeSpec(
        config_path=tmp_path / "serverfs.toml",
        auth_file=tmp_path / "private" / "core-auth.env",
        port=19876,
        hostname="core.example.test",
        user_sid=SID,
    )


@pytest.mark.parametrize(
    "hostname",
    [
        "",
        "-bad.example.com",
        "bad-.example.com",
        "api..example.com",
        ".example.com",
        "example.com.",
        "https://example.com",
        "example.com:18765",
        "localhost,example.com",
        "a" * 64 + ".example.com",
    ],
)
def test_invalid_hostnames_refused(hostname: str) -> None:
    with pytest.raises(core.WindowsCoreError, match="exactly one valid"):
        core._strict_hostname(hostname)


def test_valid_hostname_is_not_wildcarded() -> None:
    assert core._strict_hostname("core.example.test") == "core.example.test"
    assert core._strict_hostname("127.0.0.1") == "127.0.0.1"


def test_environment_drops_all_ambient_ingress_and_runtime_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path / "known")
    polluted = {
        "SYSTEMROOT": "C:/Windows",
        "LOCALAPPDATA": "C:/attacker",
        "PATH": "safe",
        "OPENAI_API_KEY": "secret",
        "MCP_EXTRA_HEADERS": "Bearer wrong-secret",
        "CONTROL_PLANE_TOKEN": "secret",
        "TUNNEL_CLIENT_TOKEN": "secret",
        "SERVERFS_AGENT_BRIDGE_SOCKET": "different-identity",
        "SERVERFS_DATA_HOME": "C:/untrusted",
        "HTTPS_PROXY": "http://proxy-credentials",
        "PYTHONPATH": "C:/user-injected-python",
        "SERVERFS_HTTP_API_KEY": "untrusted-ambient-auth",
    }
    clean = core._core_environment(
        {
            "SERVERFS_HTTP_API_KEY": "private-file-auth",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "core.example.test,127.0.0.1:19876",
        },
        source=polluted,
    )
    assert clean == {
        "SYSTEMROOT": "C:/Windows",
        "LOCALAPPDATA": str(tmp_path / "known"),
        "PATH": "safe",
        "SERVERFS_HTTP_API_KEY": "private-file-auth",
        "SERVERFS_HTTP_ALLOWED_HOSTS": "core.example.test,127.0.0.1:19876",
    }


def test_auth_environment_refuses_unknown_keys(monkeypatch, tmp_path: Path) -> None:
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path)
    with pytest.raises(core.WindowsCoreError, match="unsupported"):
        core._core_environment({"MCP_EXTRA_HEADERS": "Bearer invalid"}, source={})


def test_config_preflight_rejects_relative_path(monkeypatch) -> None:
    with pytest.raises(core.WindowsCoreError, match="absolute"):
        core._verified_config(Path("serverfs.toml"))


def test_core_preflight_rejects_wrong_sid_before_reading_auth(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import native_endpoint

    monkeypatch.setattr(core, "_ensure_windows", lambda: None)
    monkeypatch.setattr(core, "_verified_config", lambda path: (path, [path.parent / "workspace"]))
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: "S-1-5-21-other")
    monkeypatch.setattr(
        core,
        "validate_private_file",
        lambda *args, **kwargs: pytest.fail("must refuse before reading credential"),
    )
    with pytest.raises(core.WindowsCoreError, match="SID"):
        core.validate_runtime(spec)


def test_core_preflight_rejects_invalid_port_before_config_read(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_ensure_windows", lambda: None)
    monkeypatch.setattr(core, "_verified_config", lambda _: pytest.fail("must validate port first"))
    from dataclasses import replace

    with pytest.raises(core.WindowsCoreError, match="port"):
        core.validate_runtime(replace(spec, port=0))


def test_core_auth_must_be_protected_and_outside_exposed_workdir(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import native_endpoint
    from serverfs_mcp import windows_cloudflare_security as secure

    monkeypatch.setattr(core, "_ensure_windows", lambda: None)
    monkeypatch.setattr(core, "_verified_config", lambda path: (path, [path.parent]))
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: SID)
    monkeypatch.setattr(secure, "_no_reparse_components", lambda _: None)
    monkeypatch.setattr(
        core,
        "validate_private_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            secure.WindowsPrivateFileError("not private")
        ),
    )
    with pytest.raises(core.WindowsCoreError, match="not private"):
        core.validate_runtime(spec)


def test_preflight_accepts_only_allowlisted_auth_and_available_port(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import native_endpoint
    from serverfs_mcp import windows_cloudflare_security as secure

    monkeypatch.setattr(core, "_ensure_windows", lambda: None)
    monkeypatch.setattr(core, "_verified_config", lambda path: (path, [path.parent / "workspace"]))
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: SID)
    monkeypatch.setattr(secure, "_no_reparse_components", lambda _: None)
    monkeypatch.setattr(core, "validate_private_file", lambda path, **kwargs: path)
    monkeypatch.setattr(
        core,
        "_read_auth_file",
        lambda path, hostname, port: {
            "SERVERFS_HTTP_API_KEY": "private-test",
            "SERVERFS_HTTP_ALLOWED_HOSTS": f"{hostname},127.0.0.1:{port}",
        },
    )
    monkeypatch.setattr(core, "_port_available", lambda port: True)
    assert core.validate_runtime(spec, require_free_port=True)[0] == spec.config_path
    monkeypatch.setattr(core, "_port_available", lambda port: False)
    with pytest.raises(core.WindowsCoreError, match="already allocated"):
        core.validate_runtime(spec, require_free_port=True)


def test_core_auth_reuses_exact_host_and_one_auth_mode(monkeypatch, tmp_path: Path) -> None:
    from serverfs_mcp import windows_service

    monkeypatch.setattr(
        windows_service,
        "_read_env_file",
        lambda path: {
            "SERVERFS_HTTP_ALLOWED_HOSTS": "core.example.test,127.0.0.1:19876",
            "SERVERFS_HTTP_API_KEY": "sensitive-test",
        },
    )
    settings = core._read_auth_file(tmp_path / "core.env", "core.example.test", 19876)
    assert settings["SERVERFS_HTTP_ALLOWED_HOSTS"] == "core.example.test,127.0.0.1:19876"
    with pytest.raises(core.WindowsCoreError, match="allowlist"):
        core._read_auth_file(tmp_path / "core.env", "other.example.test", 19876)


def test_core_rejects_wildcard_or_missing_loopback_host(monkeypatch, tmp_path: Path) -> None:
    from serverfs_mcp import windows_service

    def check(hosts: str) -> None:
        monkeypatch.setattr(
            windows_service,
            "_read_env_file",
            lambda path: {
                "SERVERFS_HTTP_ALLOWED_HOSTS": hosts,
                "SERVERFS_HTTP_API_KEY": "sensitive-test",
            },
        )
        with pytest.raises(core.WindowsCoreError, match="allowlist"):
            core._read_auth_file(tmp_path / "core.env", "core.example.test", 19876)

    for bad in (
        "*",
        "core.example.test",
        "127.0.0.1:19876",
        "core.example.test,127.0.0.1:*",
        "core.example.test,127.0.0.1:19876,evil.example",
    ):
        check(bad)


def test_core_process_uses_existing_native_http_cli_without_bridge_spawn(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    from serverfs_mcp import cli, native_lifecycle, windows_core_control

    events = []

    @contextmanager
    def fake_lock():
        events.append("lock.acquired")
        try:
            yield
        finally:
            events.append("lock.released")

    monkeypatch.setattr(windows_core_control, "core_owner_mutex", fake_lock)
    monkeypatch.setattr(
        windows_core_control,
        "record_runtime_owner",
        lambda spec: events.append("owner.recorded"),
    )
    monkeypatch.setattr(
        core,
        "validate_runtime",
        lambda spec, **kwargs: (
            spec.config_path,
            spec.auth_file,
            {
                "SERVERFS_HTTP_API_KEY": "private-test",
                "SERVERFS_HTTP_ALLOWED_HOSTS": f"{spec.hostname},127.0.0.1:19876",
            },
        ),
    )
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path)
    # Do not change the actual pytest process environment or invoke HTTP.
    fake_env = {"OPENAI_API_KEY": "should disappear", "PATH": "safe"}
    monkeypatch.setattr(os, "environ", fake_env)
    captured: list[list[str]] = []

    def fake_cli_main(argv: list[str]) -> int:
        events.append("core.serve")
        captured.append(argv)
        assert "OPENAI_API_KEY" not in os.environ
        assert os.environ["SERVERFS_HTTP_API_KEY"] == "private-test"
        return 77

    monkeypatch.setattr(cli, "main", fake_cli_main)
    assert core.run_core(spec) == 77
    assert events == ["lock.acquired", "owner.recorded", "core.serve", "lock.released"]
    assert captured == [
        [
            "serve",
            "--config",
            str(spec.config_path),
            "--transport",
            "streamable-http",
            "--http-port",
            "19876",
        ]
    ]


def test_core_refuses_unregistered_runtime_before_serving_http(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from serverfs_mcp import cli, windows_core_control

    monkeypatch.setattr(
        core,
        "validate_runtime",
        lambda _spec, **kwargs: (
            spec.config_path,
            spec.auth_file,
            {"SERVERFS_HTTP_API_KEY": "synthetic"},
        ),
    )

    @contextmanager
    def fake_lock():
        yield

    monkeypatch.setattr(windows_core_control, "core_owner_mutex", fake_lock)
    monkeypatch.setattr(
        windows_core_control,
        "record_runtime_owner",
        lambda *_: (_ for _ in ()).throw(
            windows_core_control.CoreControlError("Core task not registered")
        ),
    )
    monkeypatch.setattr(
        cli, "main", lambda *_: pytest.fail("must not serve without registered identity")
    )
    with pytest.raises(windows_core_control.CoreControlError, match="not registered"):
        core.run_core(spec)


def test_entrypoint_rejects_other_modes_without_calling_runtime(
    spec: core.CoreRuntimeSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "run_core", lambda _: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as ex:
        core.main(["install", "--config", str(spec.config_path)])
    assert ex.value.code == 2


def test_no_legacy_supervisor_connector_or_job_ownership_in_core() -> None:
    source = Path(core.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "subprocess.Popen(",
        "WindowsJob(",
        "run_with_agent(",
        "cloudflared",
        "tunnel-client",
        "ServerFS Native Tunnel",
    ):
        assert forbidden not in source
