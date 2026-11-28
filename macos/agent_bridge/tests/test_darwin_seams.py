"""Darwin Agent Bridge seam tests (v0.13 Phase D).

These run only on macOS and exercise the real Darwin mechanisms behind the
§3 seams: getpeereid peer identity, the OS-provided runtime directory and
the derived default paths. The Linux and Windows mechanisms keep their own
coverage; nothing here weakens theirs.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest

from serverfs_agent_bridge.data_home import (
    BRIDGE_DIRECTORY,
    RUNTIME_DIRECTORY,
    SOCKET_NAME,
    bridge_data_home,
    darwin_runtime_dir,
    serverfs_data_dir,
)
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.local_ipc import (
    DarwinPeer,
    authorize_darwin_peer,
    measure_darwin_peer,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin Bridge contract")


class TestPeerIdentity:
    """getpeereid over a real AF_UNIX connection (probe 0E)."""

    @staticmethod
    async def _measure_from_connection():

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint = darwin_runtime_dir() / "serverfs-peer-probe.sock"
        try:
            server.bind(str(endpoint))
            server.listen(1)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(endpoint))
            conn, _ = server.accept()
            try:
                return measure_darwin_peer(conn)
            finally:
                client.close()
                conn.close()
        finally:
            server.close()
            try:
                endpoint.unlink()
            except FileNotFoundError:
                pass

    def test_measure_returns_current_identity(self) -> None:
        peer = asyncio.run(self._measure_from_connection())
        assert isinstance(peer, DarwinPeer)
        assert peer.uid == os.getuid()
        assert peer.gid == os.getgid()
        # Darwin fabricates no peer PID (dev_plan_v0.13.md §11 D3)
        assert not hasattr(peer, "pid")

    def test_authorization_compares_measured_uid(self) -> None:
        peer = DarwinPeer(uid=os.getuid(), gid=os.getgid())
        authorize_darwin_peer(peer, allowed_uid=os.getuid(), allowed_gid=os.getgid())
        with pytest.raises(BridgeError) as foreign:
            authorize_darwin_peer(peer, allowed_uid=os.getuid() + 1, allowed_gid=None)
        assert foreign.value.code == "PEER_NOT_AUTHORIZED"

    def test_unmeasurable_peer_is_refused(self) -> None:
        with pytest.raises(BridgeError) as refused:
            measure_darwin_peer(None)
        assert refused.value.code == "PEER_NOT_AUTHORIZED"


class TestRuntimeDir:
    def test_runtime_dir_is_owned_and_private(self) -> None:
        import stat as stat_module

        runtime = darwin_runtime_dir()
        info = runtime.lstat()
        assert stat_module.S_ISDIR(info.st_mode)
        assert info.st_uid == os.getuid()
        assert not info.st_mode & 0o022

    def test_derived_socket_shape_and_length(self) -> None:
        from serverfs_agent_bridge.config import _default_paths

        endpoint, state_dir, lock_dir = _default_paths()
        assert endpoint.endswith(f"{RUNTIME_DIRECTORY}/{SOCKET_NAME}")
        # sun_path budget (dev_plan_v0.13.md §11 D2)
        assert len(os.fsencode(endpoint)) <= 103
        home = bridge_data_home()
        assert state_dir == str(home / "state")
        assert lock_dir == str(home / "locks")

    def test_data_home_is_application_support(self) -> None:
        home = serverfs_data_dir(env={})
        expected = os.path.expanduser("~/Library/Application Support/ServerFS")
        assert str(home) == expected
        assert home.name == "ServerFS"
        assert bridge_data_home(env={}).name == BRIDGE_DIRECTORY


class TestSocketPathLimit:
    """§11 D2: a too-long endpoint is refused before bind, with a coded error."""

    def test_start_refuses_overlong_socket_path(self, tmp_path) -> None:
        from serverfs_agent_bridge.protocol import BridgeProtocolServer

        long_parent = tmp_path
        while len(os.fsencode(str(long_parent / "bridge.sock"))) < 110:
            long_parent = long_parent / "segment-to-pad-the-path-length"
        server = BridgeProtocolServer(
            service=object(),
            socket_path=long_parent / "bridge.sock",
        )
        with pytest.raises(BridgeError) as too_long:
            asyncio.run(server.start())
        assert too_long.value.code == "SOCKET_PATH_TOO_LONG"


class TestConfigDefaults:
    def test_darwin_defaults_are_derived_not_linux_paths(self, tmp_path) -> None:
        from serverfs_agent_bridge.config import BridgeConfig

        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        config = BridgeConfig.load(config_path)
        assert not str(config.socket_path).startswith("/run/")
        assert config.socket_path.name == SOCKET_NAME
        assert config.socket_path.parent.name == RUNTIME_DIRECTORY


class TestPosixStateInspector:
    """The Bridge inspector answers doctor's private-state question on POSIX too."""

    def test_cold_deployment_reports_absent(self, tmp_path) -> None:
        from serverfs_agent_bridge.inspect_state import ABSENT, inspect_private_state

        report = inspect_private_state(tmp_path)
        assert all(entry["status"] == ABSENT for entry in report.values()), report

    def test_safe_private_state_and_unsafe_modes(self, tmp_path) -> None:
        import os
        import stat as stat_module

        from serverfs_agent_bridge.inspect_state import SAFE, UNSAFE, inspect_private_state

        home = tmp_path / "agent-bridge"
        home.mkdir(mode=0o700)
        (home / "state").mkdir(mode=0o700)
        (home / "locks").mkdir(mode=0o700)
        config = home / "bridge.json"
        config.write_text("{}")
        os.chmod(config, 0o600)
        report = inspect_private_state(tmp_path)
        assert report["state"]["status"] == SAFE
        assert report["locks"]["status"] == SAFE
        assert report["config"]["status"] == SAFE
        assert report["data_home"]["status"] == SAFE

        # a world-readable state directory is unsafe, not unknown
        os.chmod(home / "state", 0o755)
        report2 = inspect_private_state(tmp_path)
        assert report2["state"]["status"] == UNSAFE
        assert "Bridge user" in report2["state"]["reason"]
        os.chmod(home / "state", 0o700)

        # a foreign-owned file is unsafe
        config.chmod(0o777)
        report3 = inspect_private_state(tmp_path)
        assert report3["config"]["status"] == UNSAFE
        config.chmod(0o600)

        # a symlinked entry is unsafe (never followed)
        (tmp_path / "outside").mkdir()
        (home / "state").rmdir()
        (home / "state").symlink_to(tmp_path / "outside")
        report4 = inspect_private_state(tmp_path)
        assert report4["state"]["status"] == UNSAFE
        assert stat_module.S_ISLNK((home / "state").lstat().st_mode)


class TestPrivateStateAncestors:
    """parents=True must give every missing ancestor the private mode (like Windows)."""

    def test_missing_middle_directories_are_private(self, tmp_path) -> None:
        from serverfs_agent_bridge.private_state import DirectoryMessages, ensure_private_directory

        target = tmp_path / "a" / "b" / "locks"
        ensure_private_directory(
            target,
            mode=0o700,
            messages=DirectoryMessages(
                not_a_directory="not a directory",
                not_owned="not owned",
            ),
            parents=True,
        )
        import os
        import stat as stat_module

        for directory in (tmp_path / "a", tmp_path / "a" / "b", target):
            info = os.lstat(directory)
            assert stat_module.S_IMODE(info.st_mode) == 0o700, directory
            assert info.st_uid == os.getuid()
