"""Linux private-state inspector is read-only and fails closed."""

from pathlib import Path

from serverfs_agent_bridge.inspect_state import ABSENT, SAFE, UNSAFE, inspect_private_state


def test_cold_state_is_absent_without_creation(tmp_path: Path):
    root = tmp_path / "not-created"
    result = inspect_private_state(root)
    assert set(result) == {"data_home", "state", "locks", "config"}
    assert all(item["status"] == ABSENT for item in result.values())
    assert not root.exists()


def test_owner_mode_type_and_dangling_links_are_reported(tmp_path: Path):
    home = tmp_path / "agent-bridge"
    home.mkdir(mode=0o700)
    (home / "state").mkdir(mode=0o700)
    (home / "locks").mkdir(mode=0o755)
    config = home / "bridge.json"
    config.symlink_to(home / "absent.json")
    before = {entry.name: entry.lstat().st_mode for entry in home.iterdir()}
    result = inspect_private_state(tmp_path)
    assert result["data_home"]["status"] == SAFE
    assert result["state"]["status"] == SAFE
    assert result["locks"]["status"] == UNSAFE
    assert result["config"]["status"] == UNSAFE
    assert {entry.name: entry.lstat().st_mode for entry in home.iterdir()} == before
    assert config.is_symlink()
    assert not (home / "absent.json").exists()
