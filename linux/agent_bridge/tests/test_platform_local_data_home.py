"""Platform-local Bridge data-home contract."""

from pathlib import Path

from serverfs_agent_bridge.data_home import bridge_data_home, serverfs_data_dir


def test_override_is_exact(tmp_path: Path):
    override = tmp_path / "override"
    env = {"SERVERFS_DATA_HOME": str(override), "HOME": str(tmp_path / "ignored")}
    assert serverfs_data_dir(env) == override
    assert bridge_data_home(env) == override / "agent-bridge"


def test_linux_xdg_and_home_defaults(tmp_path: Path):
    assert serverfs_data_dir({"HOME": str(tmp_path)}) == tmp_path / ".local/share/serverfs"
    assert serverfs_data_dir({"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "serverfs"
