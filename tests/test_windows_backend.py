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
from serverfs_mcp.errors import MutationError
from serverfs_mcp.paths import resolve_workdir_path
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
        with pytest.raises((BackendError, MutationError)) as excinfo:
            WindowsBackend().open_session(wd)
        assert excinfo.value.code == "PATH_NOT_FOUND"

    def test_out_of_scope_root_namespace_is_refused(self) -> None:
        bad = Workdir("bad", Path("\\\\.\\PhysicalDrive0"), None, read_only=True)
        with pytest.raises((BackendError, MutationError)) as excinfo:
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

    def test_mutation_channels_are_live_not_pending(self, tmp_path: Path) -> None:
        root = tmp_path / "live"
        root.mkdir()
        wd = Workdir("live", root, None, read_only=False)
        session = WindowsBackend().open_session(wd)
        resolved = resolve_workdir_path(wd, "f.txt", allow_hidden=True)
        result = session.create_file(resolved, "payload\n", max_write_bytes=128)
        assert result.created is True
        assert (root / "f.txt").read_bytes() == b"payload\n"


class TestSessionMutations:
    def _rw_session(self, tmp_path: Path) -> tuple[WindowsWorkdirSession, Workdir, Path]:
        root = tmp_path / "rw"
        root.mkdir()
        wd = Workdir("rw", root, None, read_only=False)
        session = WindowsBackend().open_session(wd)
        return session, wd, root

    @staticmethod
    def _resolve(wd: Workdir, path: str):
        return resolve_workdir_path(wd, path, allow_hidden=False)

    def test_create_revision_chains_into_stat(self, tmp_path: Path) -> None:
        session, wd, root = self._rw_session(tmp_path)
        created = session.create_file(
            self._resolve(wd, "new.txt"), "hello\r\nworld", max_write_bytes=1024
        )
        assert created.bytes_written == 12
        # exact bytes, no newline translation (frozen CRLF contract)
        assert (root / "new.txt").read_bytes() == b"hello\r\nworld"
        stat = session.stat(self._resolve(wd, "new.txt"))
        assert stat.revision == created.revision

    def test_create_existing_is_never_overwrite(self, tmp_path: Path) -> None:
        session, wd, root = self._rw_session(tmp_path)
        session.create_file(self._resolve(wd, "dup.txt"), "first", max_write_bytes=128)
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.create_file(self._resolve(wd, "dup.txt"), "second", max_write_bytes=128)
        assert excinfo.value.code == "PATH_ALREADY_EXISTS"
        assert (root / "dup.txt").read_bytes() == b"first"

    def test_missing_parent_maps_to_parent_not_found(self, tmp_path: Path) -> None:
        session, wd, _root = self._rw_session(tmp_path)
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.create_file(self._resolve(wd, "gone/x.txt"), "x", max_write_bytes=128)
        assert excinfo.value.code == "PARENT_NOT_FOUND"

    def test_edit_roundtrip_stale_revision_and_conflict(self, tmp_path: Path) -> None:
        from serverfs_mcp.models import TextEdit

        session, wd, root = self._rw_session(tmp_path)
        created = session.create_file(
            self._resolve(wd, "e.txt"), "alpha\nbeta\n", max_write_bytes=512
        )
        edited = session.replace_file(
            self._resolve(wd, "e.txt"),
            created.revision,
            [TextEdit(old_text="beta", new_text="BETA")],
            max_write_bytes=512,
            max_edits_per_call=10,
        )
        assert edited.edits_applied == 1
        assert edited.revision_before == created.revision
        assert (root / "e.txt").read_bytes() == b"alpha\nBETA\n"
        stat = session.stat(self._resolve(wd, "e.txt"))
        assert stat.revision == edited.revision
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.replace_file(
                self._resolve(wd, "e.txt"),
                created.revision,
                [TextEdit(old_text="alpha", new_text="x")],
                max_write_bytes=512,
                max_edits_per_call=10,
            )
        assert excinfo.value.code == "REVISION_CONFLICT"
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.replace_file(
                self._resolve(wd, "e.txt"),
                edited.revision,
                [TextEdit(old_text="nope", new_text="x")],
                max_write_bytes=512,
                max_edits_per_call=10,
            )
        assert excinfo.value.code == "EDIT_CONFLICT"
        assert (root / "e.txt").read_bytes() == b"alpha\nBETA\n"

    def test_edit_preserves_bom_and_crlf(self, tmp_path: Path) -> None:
        from serverfs_mcp.models import TextEdit

        session, wd, root = self._rw_session(tmp_path)
        created = session.create_file(
            self._resolve(wd, "bom.txt"), "\ufeffline1\r\nline2\r\n", max_write_bytes=512
        )
        edited = session.replace_file(
            self._resolve(wd, "bom.txt"),
            created.revision,
            [TextEdit(old_text="line2", new_text="LINE2")],
            max_write_bytes=512,
            max_edits_per_call=10,
        )
        assert (root / "bom.txt").read_bytes() == b"\xef\xbb\xbfline1\r\nLINE2\r\n"
        assert edited.bytes_after == len(b"\xef\xbb\xbfline1\r\nLINE2\r\n")

    def test_delete_returns_size_and_revision(self, tmp_path: Path) -> None:
        session, wd, root = self._rw_session(tmp_path)
        created = session.create_file(
            self._resolve(wd, "d.bin"), b"12345".decode(), max_write_bytes=64
        )
        result = session.delete_file(self._resolve(wd, "d.bin"), created.revision)
        assert result.deleted is True
        assert result.bytes_deleted == 5
        assert result.revision_deleted == created.revision
        assert not (root / "d.bin").exists()

    def test_delete_directory_requires_revision_and_physical_emptiness(
        self, tmp_path: Path
    ) -> None:
        import os

        session, wd, root = self._rw_session(tmp_path)
        created = session.create_directory(self._resolve(wd, "dir"))
        stat = session.stat(self._resolve(wd, "dir"))
        assert stat.revision == created.revision
        (root / "dir" / ".serverfs-tmp-sentinel").write_bytes(b"x")
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.delete_directory(self._resolve(wd, "dir"), created.revision)
        assert excinfo.value.code == "DIRECTORY_NOT_EMPTY"
        os.remove(root / "dir" / ".serverfs-tmp-sentinel")
        # Windows directory revisions do not move when children change
        # (frozen platform semantics), so the create-time revision is
        # still the current one and the delete now succeeds
        session.delete_directory(self._resolve(wd, "dir"), created.revision)
        assert not (root / "dir").exists()

    def test_binary_channels_report_sha_and_revision(self, tmp_path: Path) -> None:
        import hashlib

        session, wd, root = self._rw_session(tmp_path)
        data = bytes(range(256))
        created = session.create_binary_file(
            self._resolve(wd, "blob.bin"), data, max_binary_bytes=1024
        )
        assert created.sha256 == hashlib.sha256(data).hexdigest()
        assert created.revision_before is None
        assert (root / "blob.bin").read_bytes() == data
        replaced = session.replace_binary_file(
            self._resolve(wd, "blob.bin"), b"small", created.revision, max_binary_bytes=1024
        )
        assert replaced.created is False
        assert replaced.replaced is True
        assert replaced.revision_before == created.revision
        assert (root / "blob.bin").read_bytes() == b"small"
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.replace_binary_file(
                self._resolve(wd, "blob.bin"), b"x", created.revision, max_binary_bytes=1024
            )
        assert excinfo.value.code == "REVISION_CONFLICT"

    def test_oversized_payloads_and_content_gates_use_linux_codes(self, tmp_path: Path) -> None:
        session, wd, _root = self._rw_session(tmp_path)
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.create_file(self._resolve(wd, "big.txt"), "x" * 100, max_write_bytes=10)
        assert excinfo.value.code == "WRITE_TOO_LARGE"
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.create_file(self._resolve(wd, "nul.txt"), "a\x00b", max_write_bytes=100)
        assert excinfo.value.code == "BINARY_CONTENT_NOT_ALLOWED"
        with pytest.raises((BackendError, MutationError)) as excinfo:
            session.create_binary_file(self._resolve(wd, "b.bin"), b"x" * 100, max_binary_bytes=10)
        assert excinfo.value.code == "WRITE_TOO_LARGE"

    def test_read_only_session_is_refused_by_the_kernel(self, tmp_path: Path) -> None:
        from serverfs_mcp.models import TextEdit

        root = tmp_path / "ro"
        root.mkdir()
        (root / "x.txt").write_bytes(b"content\n")
        wd = Workdir("ro", root, None, read_only=True)
        session = WindowsBackend().open_session(wd)
        calls = [
            lambda: session.create_file(self._resolve(wd, "n.txt"), "x", max_write_bytes=10),
            lambda: session.replace_file(
                self._resolve(wd, "x.txt"),
                "v1:0000000000000000",
                [TextEdit(old_text="a", new_text="b")],
                max_write_bytes=10,
                max_edits_per_call=2,
            ),
            lambda: session.delete_file(self._resolve(wd, "x.txt"), "v1:0000000000000000"),
            lambda: session.create_binary_file(
                self._resolve(wd, "n.bin"), b"x", max_binary_bytes=10
            ),
            lambda: session.replace_binary_file(
                self._resolve(wd, "x.txt"), b"x", "v1:0000000000000000", max_binary_bytes=10
            ),
            lambda: session.create_directory(self._resolve(wd, "d")),
            lambda: session.delete_directory(self._resolve(wd, "d"), "v1:0000000000000000"),
        ]
        for call in calls:
            with pytest.raises((BackendError,)) as excinfo:
                call()
            assert excinfo.value.code == "WORKDIR_READ_ONLY"


def test_settings_defaults_unchanged_on_windows_path() -> None:
    # wiring must not have pulled Linux-only configuration assumptions in
    assert Settings().agent_bridge_enabled is False
