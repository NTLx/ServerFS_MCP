"""Windows Cloudflare provider validation — no real credentials or Task Scheduler."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import native_config, windows_service
from serverfs_mcp import windows_cloudflare_config as cfg


@pytest.fixture
def installation(tmp_path: Path, monkeypatch):
    data_home = tmp_path / "data"
    binary = data_home / "bin" / f"cloudflared-{cfg.VERSION}-windows-amd64" / "cloudflared.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"mock signed release")
    config = tmp_path / "serverfs.toml"
    config.write_text("[[workdirs]]\nalias='test'\npath='ignored'\n", encoding="utf-8")
    root = tmp_path / "readonly"
    root.mkdir()
    token = tmp_path / "connector.token"
    env = tmp_path / "origin.env"
    token.write_text("fixture-token", encoding="utf-8")
    env.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(cfg, "_port_available", lambda p: True)
    monkeypatch.setattr(cfg, "serverfs_data_dir", lambda: data_home)
    monkeypatch.setattr(cfg, "_no_reparse_components", lambda p: None)
    monkeypatch.setattr(cfg, "validate_private_file", lambda p, **kw: Path(p).resolve())
    monkeypatch.setattr(cfg, "_sha256", lambda p: cfg.WINDOWS_BINARY_SHA256)
    monkeypatch.setattr(cfg, "_verify_windows_binary", lambda p: None)
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda p: ([SimpleNamespace(root=root, read_only=True)], None),
    )
    monkeypatch.setattr(
        windows_service,
        "_read_env_file",
        lambda p: {
            "SERVERFS_HTTP_API_KEY": "fixture-key",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "mcp.example.invalid",
        },
    )
    return dict(
        config_path=config,
        cloudflared=binary,
        token_file=token,
        env_file=env,
        port=18765,
        hostname="mcp.example.invalid",
        user_sid="S-1-5-21-1-2-3-4",
    )


def test_accepts_pinned_binary_private_auth_and_readonly_workdir(installation):
    result = cfg.validate_install(**installation)
    assert result.config_path == installation["config_path"].resolve()
    assert result.cloudflared == installation["cloudflared"].resolve()
    assert result.port == 18765
    assert result.origin_environment["SERVERFS_HTTP_API_KEY"] == "fixture-key"
    assert "fixture-key" not in repr(result)


@pytest.mark.parametrize("host", ["-invalid.example", "a..b", "bad host", "x" * 64])
def test_rejects_bad_hostname(installation, host):
    installation["hostname"] = host
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="hostname"):
        cfg.validate_install(**installation)


def test_rejects_bad_port(installation):
    installation["port"] = 0
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="port"):
        cfg.validate_install(**installation)


def test_preserves_explicit_native_read_write_policy(installation, monkeypatch):
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda p: (
            [SimpleNamespace(root=installation["config_path"].parent, read_only=False)],
            None,
        ),
    )
    # Cloudflare is transport; Workdir permission is governed by native policy,
    # not silently widened or narrowed by choosing a different tunnel provider.
    assert cfg.validate_install(**installation).hostname == installation["hostname"]


def test_requires_at_least_one_workdir(installation, monkeypatch):
    monkeypatch.setattr(native_config, "load_native_config", lambda p: ([], None))
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="at least one"):
        cfg.validate_install(**installation)


def test_rejects_unsigned_release(installation, monkeypatch):
    def reject(p):
        raise RuntimeError("unsigned")

    monkeypatch.setattr(cfg, "_verify_windows_binary", reject)
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="validation failed"):
        cfg.validate_install(**installation)


def test_rejects_binary_digest_mismatch(installation, monkeypatch):
    monkeypatch.setattr(cfg, "_sha256", lambda p: "0" * 64)
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="digest"):
        cfg.validate_install(**installation)


def test_rejects_unsupported_environment_key(installation, monkeypatch):
    monkeypatch.setattr(
        windows_service,
        "_read_env_file",
        lambda p: {
            "SERVERFS_HTTP_API_KEY": "fixture",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "mcp.example.invalid",
            "CONTROL_PLANE_API_KEY": "never-allowed",
        },
    )
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="unsupported"):
        cfg.validate_install(**installation)


def test_rejects_missing_auth(installation, monkeypatch):
    monkeypatch.setattr(
        windows_service,
        "_read_env_file",
        lambda p: {"SERVERFS_HTTP_ALLOWED_HOSTS": "mcp.example.invalid"},
    )
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="exactly one"):
        cfg.validate_install(**installation)


def test_rejects_host_mismatch(installation, monkeypatch):
    monkeypatch.setattr(
        windows_service,
        "_read_env_file",
        lambda p: {
            "SERVERFS_HTTP_API_KEY": "fixture-key",
            "SERVERFS_HTTP_ALLOWED_HOSTS": "other.example.invalid",
        },
    )
    with pytest.raises(cfg.WindowsCloudflareConfigurationError, match="allowed hosts"):
        cfg.validate_install(**installation)
