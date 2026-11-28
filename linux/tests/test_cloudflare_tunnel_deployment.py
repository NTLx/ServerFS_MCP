"""Linux Cloudflare Tunnel Compose boundary and credential safety regression."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "compose.cloudflare.yml"


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Only a dummy *file path* is ever passed; the real token stays outside tests.
    env["CLOUDFLARE_TUNNEL_TOKEN_FILE"] = "/tmp/serverfs-test-cloudflare-token"
    env["SERVERFS_HTTP_API_KEY"] = "placeholder-only-not-a-production-key"
    env["CLOUDFLARED_UID"] = "12345"
    env["CLOUDFLARED_GID"] = "12346"
    return subprocess.run(
        ["docker", "compose", "-f", "compose.yml", "-f", OVERLAY.name, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cloudflare_overlay_static_boundaries() -> None:
    compose = OVERLAY.read_text(encoding="utf-8")
    assert 'profiles: ["openai-tunnel"]' in compose
    assert "CLOUDFLARED_IMAGE:-cloudflare/cloudflared:2026.10.7" in compose
    assert "CLOUDFLARE_TUNNEL_TOKEN_FILE:?" in compose
    assert "CLOUDFLARED_UID:-10001" in compose
    assert "CLOUDFLARED_GID:-10001" in compose
    assert "create_host_path: false" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "serverfs_internal" in compose
    assert "tunnel_egress" in compose
    assert "ports:" not in compose
    assert "TUNNEL_TOKEN:" not in compose
    assert "--token-file" in compose
    assert "CONTROL_PLANE_API_KEY" not in compose


@pytest.mark.skipif(sys.platform != "linux", reason="Cloudflare Compose runtime is Linux-only")
def test_cloudflare_overlay_render_isolated_from_openai_tunnel() -> None:
    docker_version = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    )
    if docker_version.returncode:
        pytest.skip("Docker Compose is not installed")
    result = _compose("config", "--format", "json")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    server = services["serverfs-mcp"]
    connector = services["cloudflared"]
    assert "openai-tunnel" not in services
    assert "ports" not in server
    assert "ports" not in connector
    assert connector["user"] == "12345:12346"
    assert set(connector["networks"]) == {"serverfs_internal", "tunnel_egress"}
    assert "tunnel_egress" not in server["networks"]
    assert "volumes" in connector
    token = next(
        v for v in connector["volumes"] if v["target"] == "/run/secrets/cloudflare-tunnel-token"
    )
    assert token["read_only"] is True
    # Compose v2 may omit bind.create_host_path from normalized JSON.
    # The source overlay explicitly disables implicit host-path creation.
    assert token.get("bind", {}).get("create_host_path", False) is False
    assert "/tmp/serverfs-test-cloudflare-token" == token["source"]
    assert "TUNNEL_TOKEN" not in connector.get("environment", {})
    assert "CONTROL_PLANE_API_KEY" not in connector.get("environment", {})
