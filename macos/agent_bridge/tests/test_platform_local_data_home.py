"""Platform-local Bridge data-home contract."""

from pathlib import Path

from serverfs_agent_bridge.data_home import bridge_data_home, serverfs_data_dir


def test_override_is_exact(tmp_path: Path):
    override = tmp_path / "override"
    env = {"SERVERFS_DATA_HOME": str(override), "HOME": str(tmp_path / "ignored")}
    assert serverfs_data_dir(env) == override
    assert bridge_data_home(env) == override / "agent-bridge"


def test_macos_application_support_default(tmp_path: Path):
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "ignored")}
    assert serverfs_data_dir(env) == tmp_path / "Library/Application Support/ServerFS"
