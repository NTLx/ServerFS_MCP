"""Darwin Bridge private-config policy tests that do not touch launchd or private state."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.darwin_bridge_policy import DarwinBridgePolicyError, _private_overlay
from serverfs_mcp.native_config import load_native_config


def _settings(tmp_path: Path, *, codex_proxy: bool = True):
    root = tmp_path / "repo"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(
        f"""[agent]\nenabled = true\n\n[agent.proxy]\nenabled = true\nsource = "env"\n\n"""
        f"""[agent.codex]\nenabled = true\nuse_proxy = {str(codex_proxy).lower()}\n\n"""
        f'''[[workdirs]]\nalias = "repo"\npath = "{root}"\nread_only = false\n'''
        'agent_mode = "workspace-write"\nagent_runtimes = ["codex"]\n',
        encoding="utf-8",
    )
    return load_native_config(config)[1]


def test_private_overlay_combines_same_agent_and_jev_proxy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    values = {
        "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:7897",
        "SERVERFS_JEV_API_KEY": "jev-secret",
        "SERVERFS_JEV_USE_PROXY": "true",
        "SERVERFS_PROXY_HOST": "127.0.0.1",
        "SERVERFS_PROXY_PORT": "7897",
        "SERVERFS_PROXY_USERNAME": "",
        "SERVERFS_PROXY_PASSWORD": "",
    }

    private = _private_overlay(settings, values)

    assert private["proxy"] == {"url": "http://127.0.0.1:7897", "authenticated": False}
    assert private["jev"] == {"api_key": "jev-secret", "use_proxy": True}


def test_private_overlay_rejects_different_agent_and_jev_proxy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    values = {
        "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:7897",
        "SERVERFS_JEV_API_KEY": "jev-secret",
        "SERVERFS_JEV_USE_PROXY": "true",
        "SERVERFS_PROXY_HOST": "127.0.0.1",
        "SERVERFS_PROXY_PORT": "8899",
        "SERVERFS_PROXY_USERNAME": "",
        "SERVERFS_PROXY_PASSWORD": "",
    }

    with pytest.raises(DarwinBridgePolicyError, match="different endpoints"):
        _private_overlay(settings, values)


def test_private_overlay_clears_jev_and_proxy_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, codex_proxy=False)

    private = _private_overlay(settings, {})

    assert "proxy" not in private
    assert private["jev"] == {"api_key": None, "use_proxy": False}
