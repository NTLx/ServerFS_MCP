"""Windows backend wiring tests: dispatch, session retention, pending channels.

Requires a win32 host with the ``serverfs_windows_native`` extension
installed (development: place the built ``.pyd`` on ``PYTHONPATH``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from serverfs_mcp.backends import BackendError, WorkdirSession
from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import Workdir

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native kernel")

pytest.importorskip(
    "serverfs_windows_native", reason="serverfs-windows-native wheel/pyd not installed"
)

from serverfs_mcp import backends  # noqa: E402
from serverfs_mcp.windows_backend import WindowsBackend, WindowsWorkdirSession  # noqa: E402


def _protocol_methods() -> set[str]:
    # duplicated from test_backends deliberately: that module imports the
    # Linux kernel and cannot load on a Windows host
    return {
        name
        for name, member in vars(WorkdirSession).items()
        if callable(member) and not name.startswith("_")
    }


def make_workdir(tmp_path: Path, alias: str = "repo") -> Workdir:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Workdir(alias, tmp_path, None, read_only=True)


class TestDispatch:
    def test_get_backend_returns_windows_singleton(self, tmp_path: Path) -> None:
        backend = backends.get_backend()
        assert isinstance(backend, WindowsBackend)
        assert backend is backends.get_backend()
        assert backend is WindowsBackend.shared()

    def test_session_is_retained_per_workdir(self, tmp_path: Path) -> None:
        backend = WindowsBackend()
        wd = make_workdir(tmp_path)
        first = backend.open_session(wd)
        assert backend.open_session(wd) is first, "open_session must not reopen the root"
        assert isinstance(first, WindowsWorkdirSession)

    def test_distinct_workdirs_get_distinct_sessions(self, tmp_path: Path) -> None:
        backend = WindowsBackend()
        a = backend.open_session(make_workdir(tmp_path / "a", alias="a"))
        b = backend.open_session(make_workdir(tmp_path / "b", alias="b"))
        assert a is not b

    def test_read_only_capability_is_part_of_session_identity(self, tmp_path: Path) -> None:
        root = tmp_path / "cap"
        rw = Workdir("cap", root, None, read_only=False)
        ro = Workdir("cap", root, None, read_only=True)
        backend = WindowsBackend()
        with pytest.raises(BackendError):
            backend.open_session(rw)  # root does not exist yet: fail before cache
        root.mkdir()
        session_ro = backend.open_session(ro)
        session_rw = backend.open_session(rw)
        assert session_ro is backend.open_session(ro)
        assert session_rw is not session_ro
        assert session_rw.workdir.read_only is False

    def test_identity_token_survives_external_root_rename(self, tmp_path: Path) -> None:
        # The session holds the object, not the path: renaming the root
        # directory from outside changes nothing for the retained handle.
        root = tmp_path / "live"
        root.mkdir()
        backend = WindowsBackend()
        session = backend.open_session(make_workdir(root))
        token_before = session.identity_token()
        moved = tmp_path / "moved"
        root.rename(moved)
        try:
            assert session.identity_token() == token_before
        finally:
            moved.rename(root)


class TestErrorNormalization:
    def test_missing_root_maps_to_backend_error(self, tmp_path: Path) -> None:
        wd = Workdir("gone", tmp_path / "absent", None, read_only=True)
        with pytest.raises(BackendError) as excinfo:
            WindowsBackend().open_session(wd)
        assert excinfo.value.code == "PATH_NOT_FOUND"

    def test_out_of_scope_root_namespace_is_refused(self) -> None:
        bad = Workdir("bad", Path("\\\\.\\PhysicalDrive0"), None, read_only=True)
        with pytest.raises(BackendError) as excinfo:
            WindowsBackend().open_session(bad)
        assert excinfo.value.code == "INVALID_ROOT"


class TestPendingChannels:
    def test_windows_session_covers_the_protocol(self) -> None:
        missing = [
            name
            for name in _protocol_methods()
            if not callable(getattr(WindowsWorkdirSession, name, None))
        ]
        assert not missing, f"WindowsWorkdirSession lacks protocol members: {missing}"

    def test_pending_channel_reports_structured_code(self, tmp_path: Path) -> None:
        session = WindowsBackend().open_session(make_workdir(tmp_path))
        with pytest.raises(BackendError) as excinfo:
            session.create_file(None, "x", max_write_bytes=10)
        assert excinfo.value.code == "WINDOWS_KERNEL_PENDING"


def test_settings_defaults_unchanged_on_windows_path() -> None:
    # wiring must not have pulled Linux-only configuration assumptions in
    assert Settings().agent_bridge_enabled is False
