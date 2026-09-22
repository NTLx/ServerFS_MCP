"""v0.5 Compose isolation contract for optional file ingress."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_file_ingress_sidecar_is_opt_in_and_has_no_workdir_mounts() -> None:
    text = (_REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    ingress = text.split("  serverfs-file-ingress:\n", 1)[1].split("  openai-tunnel:\n", 1)[0]

    assert 'profiles: ["file-ingress"]' in ingress
    assert 'command: ["python", "-m", "serverfs_mcp.file_ingress"]' in ingress
    assert "SERVERFS_FILE_INGRESS_ALLOWED_HOSTS" in ingress
    assert "SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS" in ingress
    assert "serverfs_file_ingress" in ingress
    assert "file_ingress_egress" in ingress
    assert "ports:" not in ingress
    assert "volumes:" not in ingress
    assert "/workdirs/" not in ingress
    assert "WORKDIR_" not in ingress
    assert "CONTROL_PLANE_" not in ingress
    assert "read_only: true" in ingress
    assert "cap_drop:\n      - ALL" in ingress
    assert "no-new-privileges:true" in ingress


def test_serverfs_mcp_joins_only_internal_networks() -> None:
    text = (_REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    server = text.split("  serverfs-mcp:\n", 1)[1].split("  serverfs-file-ingress:\n", 1)[0]
    networks = text.split("networks:\n", 1)[1]

    assert "      - serverfs_internal" in server
    assert "      - serverfs_file_ingress" in server
    assert "tunnel_egress" not in server
    assert "file_ingress_egress" not in server
    assert "  serverfs_internal:\n    internal: true" in networks
    assert "  serverfs_file_ingress:\n    internal: true" in networks


def test_openai_tunnel_cannot_call_file_ingress_network_directly() -> None:
    text = (_REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    tunnel = text.split("  openai-tunnel:\n", 1)[1].split("\n\nnetworks:\n", 1)[0]

    assert "      - serverfs_internal" in tunnel
    assert "      - tunnel_egress" in tunnel
    assert "serverfs_file_ingress" not in tunnel
    assert "file_ingress_egress" not in tunnel


def test_env_example_keeps_file_ingress_disabled_by_default() -> None:
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert text.count("SERVERFS_FILE_INGRESS_ENABLED=false") == 1
    assert text.count("SERVERFS_FILE_INGRESS_ALLOWED_HOSTS=") == 1
    assert text.count("SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=false") == 1
    assert "SERVERFS_FILE_INGRESS_URL" not in text
