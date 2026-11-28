from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import uvicorn

from serverfs_mcp.config import Settings

_TARGET_PATH = Path(__file__).parents[1] / "deployment" / "conformance_target.py"
_TARGET_SPEC = importlib.util.spec_from_file_location("conformance_target", _TARGET_PATH)
assert _TARGET_SPEC is not None and _TARGET_SPEC.loader is not None
target = importlib.util.module_from_spec(_TARGET_SPEC)
sys.modules[_TARGET_SPEC.name] = target
_TARGET_SPEC.loader.exec_module(target)


def test_target_constructs_empty_registry_and_exact_http_policy(monkeypatch) -> None:
    monkeypatch.setenv("WORKDIR_01_ALIAS", "unexpected")
    monkeypatch.setenv("WORKDIR_01_ROOT", "/tmp/unexpected")
    monkeypatch.setenv("SERVERFS_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVERFS_HTTP_PORT", "9999")
    monkeypatch.setattr(target, "create_server", lambda settings, registry: (settings, registry))

    settings, registry = target.create_target("127.0.0.1", 8765, "/mcp")

    assert isinstance(settings, Settings)
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 8765
    assert settings.http_path == "/mcp"
    assert settings.http_allowed_hosts == ("127.0.0.1:8765",)
    assert settings.http_allowed_origins == ("http://127.0.0.1:8765",)
    assert registry.all_workdirs() == ()


def test_target_brackets_ipv6_authority() -> None:
    settings, registry = target.create_target("::1", 8765, "/mcp")
    assert settings.http_allowed_hosts == ("[::1]:8765",)
    assert settings.http_allowed_origins == ("http://[::1]:8765",)
    assert registry.all_workdirs() == ()


def test_main_rejects_non_loopback_host() -> None:
    try:
        target.main(["--host", "192.0.2.1"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("non-loopback host was accepted")


def test_main_rejects_localhost_and_hostnames() -> None:
    for host in ("localhost", "example.test"):
        try:
            target.main(["--host", host])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"non-IP host {host!r} was accepted")


def test_main_wires_app_and_settings_to_uvicorn(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Registry:
        def all_workdirs(self):
            return []

    registry = Registry()
    app = object()

    class FakeMCP:
        def streamable_http_app(self, **kwargs):
            captured["app_kwargs"] = kwargs
            return app

    monkeypatch.setattr(target, "WorkdirRegistry", lambda workdirs: registry)

    def create_server(given_settings, given_registry):
        captured["settings"] = given_settings
        captured["registry"] = given_registry
        return FakeMCP()

    monkeypatch.setattr(target, "create_server", create_server)

    class FakeConfig:
        def __init__(self, given_app, **kwargs):
            captured["app"] = given_app
            captured["config_kwargs"] = kwargs

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        async def serve(self):
            captured["served"] = True

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert target.main(["--port", "9001", "--path", "/check"]) == 0
    assert captured["settings"].http_host == "127.0.0.1"
    assert captured["settings"].http_port == 9001
    assert captured["settings"].http_path == "/check"
    assert captured["registry"] is registry
    assert captured["app"] is app
    assert captured["app_kwargs"]["streamable_http_path"] == "/check"
    assert captured["app_kwargs"]["stateless_http"] is False
    assert captured["app_kwargs"]["host"] == "127.0.0.1"
    security = captured["app_kwargs"]["transport_security"]
    assert security.allowed_hosts == ["127.0.0.1:9001"]
    assert security.allowed_origins == ["http://127.0.0.1:9001"]
    assert captured["config_kwargs"] == {
        "host": "127.0.0.1",
        "port": 9001,
        "access_log": False,
    }
    assert captured["served"] is True
