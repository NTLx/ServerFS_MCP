from __future__ import annotations

from types import SimpleNamespace

import uvicorn

import serverfs_mcp.main as main_module
from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import WorkdirError


def test_main_reports_settings_configuration_error_without_traceback(monkeypatch, capsys) -> None:
    def fail(_env):
        raise ValueError("invalid SERVERFS_MAX_READ_LINES value")

    monkeypatch.setattr(main_module, "settings_from_env", fail)

    assert main_module.main() == 2
    captured = capsys.readouterr()
    assert "ServerFS: configuration error: invalid SERVERFS_MAX_READ_LINES value" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_workdir_configuration_error_without_traceback(monkeypatch, capsys) -> None:
    def fail(_env, _settings):
        raise WorkdirError("slot 01: invalid workdir")

    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: Settings())
    monkeypatch.setattr(main_module, "build_registry_from_env", fail)

    assert main_module.main() == 2
    captured = capsys.readouterr()
    assert "ServerFS: configuration error: slot 01: invalid workdir" in captured.err
    assert "Traceback" not in captured.err


def test_main_wires_streamable_http_transport_security(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def all_workdirs(self):
            return []

    class Server:
        def run(self, transport: str, **kwargs) -> None:
            captured["transport"] = transport
            captured.update(kwargs)

    registry = Registry()
    settings = Settings()
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: settings)
    monkeypatch.setattr(main_module, "build_registry_from_env", lambda _env, _settings: registry)
    monkeypatch.setattr(main_module, "log_startup", lambda _settings, _registry: None)
    monkeypatch.setattr(
        main_module,
        "create_server",
        lambda _settings, _registry, _agent_client, _file_ingress_client: Server(),
    )

    assert main_module.main() == 0
    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8000
    assert captured["streamable_http_path"] == "/mcp"
    assert captured["max_request_body_size"] == main_module.DEFAULT_MAX_REQUEST_BODY_SIZE
    security = captured["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["serverfs-mcp:8000"]
    assert security.allowed_origins == []


def test_main_wires_configured_streamable_http_listener(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def all_workdirs(self):
            return []

    class Server:
        def run(self, transport: str, **kwargs) -> None:
            captured["transport"] = transport
            captured.update(kwargs)

    registry = Registry()
    settings = Settings(
        http_host="127.0.0.1",
        http_port=9000,
        http_path="/serverfs",
        http_allowed_hosts=("mcp.example.com", "mcp.example.com:*"),
        http_allowed_origins=("https://app.example.com",),
    )
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: settings)
    monkeypatch.setattr(main_module, "build_registry_from_env", lambda _env, _settings: registry)
    monkeypatch.setattr(main_module, "log_startup", lambda _settings, _registry: None)
    monkeypatch.setattr(
        main_module,
        "create_server",
        lambda _settings, _registry, _agent_client, _file_ingress_client: Server(),
    )

    assert main_module.main() == 0
    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000
    assert captured["streamable_http_path"] == "/serverfs"
    security = captured["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com", "mcp.example.com:*"]
    assert security.allowed_origins == ["https://app.example.com"]


def test_http_auth_mode_is_non_secret() -> None:
    assert main_module.http_auth_mode(Settings()) == "none"
    assert main_module.http_auth_mode(Settings(http_api_key="secret")) == "api_key"
    assert main_module.http_auth_mode(Settings(oauth_enabled=True)) == "oauth"


def test_startup_log_reports_mode_without_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def list_result(self):
            return SimpleNamespace(workdirs=[])

        def all_workdirs(self):
            return []

    def capture(event: str, **kwargs) -> None:
        captured["event"] = event
        captured.update(kwargs)

    secret = "startup-log-sentinel-secret"
    monkeypatch.setattr(main_module.jsonlog, "info", capture)
    main_module.log_startup(Settings(http_api_key=secret), Registry())

    assert captured["event"] == "startup"
    assert captured["http_auth_mode"] == "api_key"
    assert secret not in repr(captured)


def test_main_routes_api_key_mode_through_protected_http_runner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def all_workdirs(self):
            return []

    class Server:
        def run(self, transport: str, **kwargs) -> None:
            raise AssertionError("API key mode must not use the unwrapped SDK runner")

    registry = Registry()
    settings = Settings(http_api_key="test-secret")
    server = Server()
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: settings)
    monkeypatch.setattr(main_module, "build_registry_from_env", lambda _env, _settings: registry)
    monkeypatch.setattr(main_module, "log_startup", lambda _settings, _registry: None)
    monkeypatch.setattr(
        main_module,
        "create_server",
        lambda _settings, _registry, _agent_client, _file_ingress_client: server,
    )

    def run_protected(_server, _settings, _registry) -> None:
        captured["server"] = _server
        captured["settings"] = _settings
        captured["registry"] = _registry

    monkeypatch.setattr(main_module, "run_api_key_streamable_http", run_protected)

    assert main_module.main() == 0
    assert captured == {"server": server, "settings": settings, "registry": registry}


def test_api_key_runner_disables_uvicorn_access_log(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def all_workdirs(self):
            return []

    class Server:
        def streamable_http_app(self, **kwargs):
            captured["app_kwargs"] = kwargs
            return object()

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            captured["app"] = app
            captured["config_kwargs"] = kwargs

    class FakeUvicornServer:
        def __init__(self, config) -> None:
            captured["config"] = config

        async def serve(self) -> None:
            captured["served"] = True

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeUvicornServer)

    settings = Settings(
        http_host="127.0.0.1",
        http_port=9000,
        http_path="/serverfs",
        http_allowed_hosts=("mcp.example.com",),
        http_api_key="test-secret",
    )
    main_module.run_api_key_streamable_http(Server(), settings, Registry())

    assert captured["served"] is True
    config_kwargs = captured["config_kwargs"]
    assert config_kwargs["host"] == "127.0.0.1"
    assert config_kwargs["port"] == 9000
    assert config_kwargs["access_log"] is False
    app_kwargs = captured["app_kwargs"]
    assert app_kwargs["streamable_http_path"] == "/serverfs"
    assert app_kwargs["host"] == "127.0.0.1"
    security = app_kwargs["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com"]


def test_streamable_http_body_limit_covers_largest_base64_workdir() -> None:
    max_raw = 8_388_608
    registry = SimpleNamespace(
        all_workdirs=lambda: [
            SimpleNamespace(
                policy=SimpleNamespace(
                    binary_transfer_enabled=True,
                    max_binary_transfer_bytes=max_raw,
                )
            )
        ]
    )
    expected = 4 * ((max_raw + 2) // 3) + main_module._MCP_REQUEST_JSON_OVERHEAD
    assert main_module.streamable_http_max_request_body_size(registry) == expected


def test_streamable_http_body_limit_ignores_binary_disabled_workdir() -> None:
    registry = SimpleNamespace(
        all_workdirs=lambda: [
            SimpleNamespace(
                policy=SimpleNamespace(
                    binary_transfer_enabled=False,
                    max_binary_transfer_bytes=64 * 1024 * 1024,
                )
            )
        ]
    )
    assert (
        main_module.streamable_http_max_request_body_size(registry)
        == main_module.DEFAULT_MAX_REQUEST_BODY_SIZE
    )
