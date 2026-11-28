"""Platform-local Bridge data-home contract."""

from pathlib import Path

import pytest

from serverfs_agent_bridge.data_home import bridge_data_home, serverfs_data_dir
from serverfs_agent_bridge.errors import BridgeError


def test_override_is_exact(tmp_path: Path):
    override = tmp_path / "override"
    env = {"SERVERFS_DATA_HOME": str(override), "HOME": str(tmp_path / "ignored")}
    assert serverfs_data_dir(env) == override
    assert bridge_data_home(env) == override / "agent-bridge"


def test_windows_localappdata_default_and_missing_value(tmp_path: Path):
    assert serverfs_data_dir({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "ServerFS"
    with pytest.raises(BridgeError) as exc:
        serverfs_data_dir({"HOME": str(tmp_path)})
    assert exc.value.code == "DATA_HOME_UNAVAILABLE"
