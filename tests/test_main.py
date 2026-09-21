from __future__ import annotations

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
    monkeypatch.setattr(main_module, "settings_from_env", lambda _env: Settings())
    monkeypatch.setattr(main_module, "build_registry_from_env", lambda _env, _settings: registry)
    monkeypatch.setattr(main_module, "log_startup", lambda _settings, _registry: None)
    monkeypatch.setattr(
        main_module,
        "create_server",
        lambda _settings, _registry, _client: Server(),
    )

    assert main_module.main() == 0
    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8000
    assert captured["streamable_http_path"] == "/mcp"
    assert captured["transport_security"] is main_module.STREAMABLE_HTTP_TRANSPORT_SECURITY
    security = main_module.STREAMABLE_HTTP_TRANSPORT_SECURITY
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["serverfs-mcp:8000"]
    assert security.allowed_origins == []
