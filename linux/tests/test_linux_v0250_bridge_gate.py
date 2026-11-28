"""Pin the Linux v0.25.0 owned-Bridge contract blockers until source support exists."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_linux_bridge_service_is_still_singleton_qualified() -> None:
    unit = (ROOT / "deployment/agent-bridge/serverfs-agent-bridge.service").read_text()
    installer = (ROOT / "deployment/agent-bridge/install.sh").read_text()

    assert "serverfs-agent-bridge.service" in installer
    assert "serverfs-agent-bridge@" not in installer
    assert "config.json" in unit
    assert "%i" not in unit
    assert 'SOCKET_DIR="$RUNTIME_ROOT/socket"' in installer
    assert 'LOCK_DIR="$RUNTIME_ROOT/locks"' in installer
    assert 'STATE_DIR="$HOME/.local/state/serverfs-agent-bridge"' in installer


def test_linux_v2_is_opt_in_and_uses_existing_authorization_seams() -> None:
    config = (ROOT / "agent_bridge/src/serverfs_agent_bridge/config.py").read_text()
    main = (ROOT / "agent_bridge/src/serverfs_agent_bridge/main.py").read_text()
    client = (ROOT / "src/serverfs_mcp/core_bridge_client_v2.py").read_text()
    ipc = (ROOT / "agent_bridge/src/serverfs_agent_bridge/local_ipc.py").read_text()
    protocol = (ROOT / "agent_bridge/src/serverfs_agent_bridge/protocol.py").read_text()

    assert '"bridge_id"' in config
    assert 'data.get("protocol_mode", "v1")' in config
    # The platform split narrows the old shared POSIX gate to Linux.
    # Behavioral foreign-host refusal is also exercised in the local client/config suites.
    assert 'if not sys.platform.startswith("linux"):' in config
    assert 'if not sys.platform.startswith("linux"):' in main
    assert 'if not sys.platform.startswith("linux"):' in client
    assert "allowed_peer_uid" in protocol and "allowed_peer_gid" in protocol
    assert "SO_PEERCRED" in ipc
    assert "owner_core_id" in protocol  # task ownership is supplied by the gated grant path
    assert "if self.v2_only:" in protocol
    assert "core_grant_verifier" in protocol


def test_instance_preflight_extends_existing_phase_e_uid_gate() -> None:
    renderer = (ROOT / "deployment/agent-bridge/render_config.py").read_text()
    linux_readme = (ROOT / "deployment/linux/README.md").read_text()

    assert "container_uid != os.getuid() or peer_uid != os.getuid()" in renderer
    assert "container_gid != os.getgid() or peer_gid != os.getgid()" in renderer
    assert "linux_v0250_preflight" in linux_readme
    assert "host/container" in linux_readme


def test_linux_compose_keeps_bridge_disabled_unless_signed_v2_overlay_is_selected() -> None:
    disabled = (ROOT / "deployment/linux/compose.bridge-disabled.yml").read_text()
    agent = (ROOT / "compose.agent.yml").read_text()
    linux_readme = (ROOT / "deployment/linux/README.md").read_text()

    assert 'SERVERFS_AGENT_BRIDGE_ENABLED: "false"' in disabled
    assert "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR" not in disabled
    assert "SERVERFS_AGENT_BRIDGE_HOST_SOCKET_DIR" in agent
    assert "SERVERFS_AGENT_LOCK_HOST_DIR" in agent
    assert "SERVERFS_AGENT_BRIDGE_STATE_DIR" not in agent
    v2 = (ROOT / "deployment/linux/compose.bridge-v2.yml").read_text()
    assert 'SERVERFS_AGENT_BRIDGE_ENABLED: "true"' in v2
    assert "SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE" in v2
    assert "SERVERFS_LINUX_INSTANCE_ID" in v2
    assert "SERVERFS_LINUX_BRIDGE_ID" in v2
    assert "WORKDIR_01_AGENT_MODE" in v2
    assert "WORKDIR_01_REPO_OPS_ENABLED" in v2
    assert "Linux v2" in linux_readme and "deployment attachment" in linux_readme
    assert "live multi-Core acceptance" in linux_readme


def test_linux_user_bridge_template_uses_instance_config_without_installing_credentials() -> None:
    template = (ROOT / "deployment/linux/serverfs-agent-bridge@.service").read_text()
    assert "[Install]" in template
    assert "WantedBy=default.target" in template
    assert "%h/.config/serverfs-agent-bridge/instances/%i/config.json" in template
    assert template.count("%h/.local/share/serverfs-agent-bridge/instances/%i/current") == 2
    assert "%h/.local/share/serverfs-agent-bridge/current" not in template
    assert "--config" in template
    assert "EnvironmentFile" not in template
    assert "User=" not in template
    assert "ExecStartPre" not in template
