from __future__ import annotations

from types import SimpleNamespace

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
        lambda _settings, _registry, _agent_client, _file_ingress_client: Server(),
    )

    assert main_module.main() == 0
    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8000
    assert captured["streamable_http_path"] == "/mcp"
    assert captured["max_request_body_size"] == main_module.DEFAULT_MAX_REQUEST_BODY_SIZE
    assert captured["transport_security"] is main_module.STREAMABLE_HTTP_TRANSPORT_SECURITY
    security = main_module.STREAMABLE_HTTP_TRANSPORT_SECURITY
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["serverfs-mcp:8000"]
    assert security.allowed_origins == []


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
