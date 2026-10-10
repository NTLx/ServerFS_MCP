"""Darwin filesystem backend tests (v0.13 Phase B).

These run only on macOS and exercise the real Darwin kernel path:
descriptor-relative traversal, fcopyfile metadata preservation, the
FD-secure search walk and the platform gate. The Linux/Windows kernels
keep their own coverage; nothing here weakens theirs.
"""

from __future__ import annotations

import base64  # noqa: F401  (kept for symmetry with future payload fixtures)
import os
import pwd
import stat as stat_module
import subprocess

import pytest

from serverfs_mcp.backends import BackendError, get_backend
from serverfs_mcp.darwin_platform import (
    REASON_NOT_ARM64,
    REASON_NOT_DARWIN,
    REASON_ROSETTA,
    REASON_WRONG_MACOS,
    darwin_platform_status,
)
from serverfs_mcp.models import TextEdit
from serverfs_mcp.paths import (
    SymlinkNotAllowedError,
    resolve_workdir_path,
)

pytestmark = pytest.mark.skipif(
    not (os.uname().sysname == "Darwin" and os.uname().machine == "arm64"),
    reason="Darwin arm64 kernel contract",
)


def _resolved(workdir, rel: str, *, allow_hidden: bool = False):
    return resolve_workdir_path(workdir, rel, allow_hidden=allow_hidden)


@pytest.fixture()
def session(workdir):
    from serverfs_mcp.darwin_backend import DarwinBackend

    return DarwinBackend().open_session(workdir)


class TestPlatformGate:
    """The measured gate passes on this machine and rejects the rest."""

    def test_this_machine_is_supported(self) -> None:
        status = darwin_platform_status()
        assert status.supported, status.reason
        assert status.machine == "arm64"
        assert status.macos_version.startswith("27.")
        assert status.rosetta is False

    def test_rejects_non_darwin(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate.sys, "platform", "linux")
        status = gate.darwin_platform_status()
        assert not status.supported
        assert status.reason == REASON_NOT_DARWIN

    def test_rejects_x86_64(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
        status = gate.darwin_platform_status()
        assert not status.supported
        assert status.reason == REASON_NOT_ARM64

    def test_rejects_macos_26(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate, "_macos_version", lambda: "26.4")
        status = gate.darwin_platform_status()
        assert not status.supported
        assert status.reason == REASON_WRONG_MACOS

    def test_rejects_macos_28(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate, "_macos_version", lambda: "28.0")
        status = gate.darwin_platform_status()
        assert not status.supported
        assert status.reason == REASON_WRONG_MACOS

    def test_rejects_rosetta(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate, "_rosetta_translated", lambda: True)
        status = gate.darwin_platform_status()
        assert not status.supported
        assert status.reason == REASON_ROSETTA

    def test_unmeasurable_rosetta_fails_closed(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate, "_rosetta_translated", lambda: None)
        status = gate.darwin_platform_status()
        assert not status.supported

    def test_patch_release_inside_27_is_allowed(self, monkeypatch) -> None:
        import serverfs_mcp.darwin_platform as gate

        monkeypatch.setattr(gate, "_macos_version", lambda: "27.9.3")
        assert gate.darwin_platform_status().supported

    def test_dispatch_returns_darwin_backend(self) -> None:
        from serverfs_mcp.darwin_backend import DarwinBackend

        assert isinstance(get_backend(), DarwinBackend)


class TestSessionContract:
    def test_session_covers_the_protocol(self, session) -> None:
        from serverfs_mcp.backends import WorkdirSession

        expected = {
            name
            for name, member in vars(WorkdirSession).items()
            if callable(member) and not name.startswith("_")
        }
        missing = [name for name in expected if not callable(getattr(session, name, None))]
        assert not missing, f"DarwinWorkdirSession lacks protocol members: {missing}"


class TestReadChannels:
    def test_stat_list_find_roundtrip(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "docs").mkdir()
        (root / "docs" / "a.txt").write_text("hello\nworld\n")
        (root / "docs" / "b.log").write_text("skip\n")

        st = session.stat(_resolved(workdir, "docs/a.txt"))
        assert st.type == "file"
        assert st.size == 12
        assert st.revision.startswith("v1:")

        entries, has_more = session.list(_resolved(workdir, "docs"), offset=0, limit=10)
        assert [e.name for e in entries] == ["a.txt", "b.log"]
        assert not has_more

        found, truncated = session.find(
            _resolved(workdir, ""), pattern="*.txt", limit=10, max_walk_entries=1000
        )
        assert found == ["docs/a.txt"]
        assert not truncated

    def test_read_text_page_and_revision(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "page.txt").write_text("one\ntwo\nthree\n")
        page = session.read_text_page(
            _resolved(workdir, "page.txt"),
            start_line=2,
            max_lines=1,
            max_read_bytes=4096,
            binary_sample=64,
        )
        assert page.lines == [b"two\n"]
        assert page.end_line == 2
        assert page.has_more

    def test_read_binary(self, session, workdir) -> None:
        root = workdir.container_path
        payload = bytes(range(256)) * 4
        (root / "blob.bin").write_bytes(payload)
        read = session.read_binary(_resolved(workdir, "blob.bin"), max_bytes=65536)
        assert read.data == payload
        assert read.size == len(payload)
        assert read.revision.startswith("v1:")


class TestMutations:
    def test_create_edit_delete_roundtrip(self, session, workdir) -> None:
        target = _resolved(workdir, "roundtrip.txt")
        created = session.create_file(target, "alpha\n", max_write_bytes=4096)
        assert created.created
        rev1 = created.revision

        st = session.stat(target)
        assert st.revision == rev1

        edited = session.replace_file(
            target,
            rev1,
            [TextEdit(old_text="alpha", new_text="beta", expected_count=1)],
            max_write_bytes=4096,
            max_edits_per_call=8,
        )
        assert edited.edited
        assert (workdir.container_path / "roundtrip.txt").read_text() == "beta\n"

        deleted = session.delete_file(target, edited.revision)
        assert deleted.deleted
        assert not (workdir.container_path / "roundtrip.txt").exists()

    def test_revision_conflict_aborts(self, session, workdir) -> None:
        from serverfs_mcp.errors import RevisionConflictError

        target = _resolved(workdir, "cas.txt")
        session.create_file(target, "v1\n", max_write_bytes=4096)
        with pytest.raises(RevisionConflictError):
            session.replace_file(
                target,
                "v1:notthere00000000",
                [TextEdit(old_text="v1", new_text="v2", expected_count=1)],
                max_write_bytes=4096,
                max_edits_per_call=8,
            )
        assert (workdir.container_path / "cas.txt").read_text() == "v1\n"

    def test_hardlink_edit_rejected(self, session, workdir) -> None:
        from serverfs_mcp.errors import MultipleHardlinksError

        root = workdir.container_path
        (root / "hard.txt").write_text("hard\n")
        os.link(root / "hard.txt", root / "hard2.txt")
        target = _resolved(workdir, "hard.txt")
        rev = session.stat(target).revision
        with pytest.raises(MultipleHardlinksError):
            session.replace_file(
                target,
                rev,
                [TextEdit(old_text="hard", new_text="soft", expected_count=1)],
                max_write_bytes=4096,
                max_edits_per_call=8,
            )

    def test_create_directory_and_delete_directory(self, session, workdir) -> None:
        from serverfs_mcp.errors import DirectoryNotEmptyError

        made = session.create_directory(_resolved(workdir, "newdir"))
        assert made.created
        (workdir.container_path / "newdir" / "inner.txt").write_text("x")
        rev = session.stat(_resolved(workdir, "newdir")).revision
        with pytest.raises(DirectoryNotEmptyError):
            session.delete_directory(_resolved(workdir, "newdir"), rev)
        os.unlink(workdir.container_path / "newdir" / "inner.txt")
        # unlinking the child changed the directory's ctime: re-read
        rev = session.stat(_resolved(workdir, "newdir")).revision
        result = session.delete_directory(_resolved(workdir, "newdir"), rev)
        assert result.deleted

    def test_binary_create_and_replace(self, session, workdir) -> None:
        target = _resolved(workdir, "binary.bin")
        data1 = b"\x00\x01\x02binary"
        created = session.create_binary_file(target, data1, max_binary_bytes=8192)
        assert created.created
        rev = created.revision
        assert isinstance(created.sha256, str) and len(created.sha256) == 64
        data2 = b"replaced\x00payload"
        replaced = session.replace_binary_file(target, data2, rev, max_binary_bytes=8192)
        assert replaced.replaced
        assert (workdir.container_path / "binary.bin").read_bytes() == data2


def _set_acl(path, entry: str) -> None:
    subprocess.run(["chmod", "+a", entry, str(path)], check=True)


def _has_acl(path) -> bool:
    out = subprocess.run(["ls", "-le", str(path)], capture_output=True, text=True).stdout
    return " allow " in out


class TestMetadataPreservation:
    """fcopyfile(COPYFILE_METADATA) carries mode/xattrs/ACL onto the new inode."""

    def test_edit_preserves_mode_xattr_acl_content(self, session, workdir) -> None:
        import ctypes
        import ctypes.util

        root = workdir.container_path
        target = root / "meta.txt"
        target.write_text("original\n")
        os.chmod(target, 0o604)
        user = pwd.getpwuid(os.getuid()).pw_name
        _set_acl(target, f"{user} allow read,write")

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fd = os.open(target, os.O_RDONLY)
        try:
            value = b"KEEP"
            rc = libc.fsetxattr(fd, b"user.probe.meta", value, len(value), 0, 0)
            assert rc == 0, ctypes.get_errno()
        finally:
            os.close(fd)

        assert _has_acl(target)
        rev = session.stat(_resolved(workdir, "meta.txt")).revision
        session.replace_file(
            _resolved(workdir, "meta.txt"),
            rev,
            [TextEdit(old_text="original", new_text="edited", expected_count=1)],
            max_write_bytes=4096,
            max_edits_per_call=8,
        )
        st = os.stat(target)
        assert stat_module.S_IMODE(st.st_mode) == 0o604
        assert (target).read_text() == "edited\n"
        fd = os.open(target, os.O_RDONLY)
        try:
            buf = ctypes.create_string_buffer(64)
            n = libc.fgetxattr(fd, b"user.probe.meta", buf, 64, 0, 0)
            assert n >= 0
            assert buf.raw[:n] == b"KEEP"
        finally:
            os.close(fd)
        assert _has_acl(target)

    def test_replace_binary_preserves_metadata(self, session, workdir) -> None:
        root = workdir.container_path
        target = root / "meta.bin"
        target.write_bytes(b"\x00old")
        os.chmod(target, 0o640)
        rev = session.stat(_resolved(workdir, "meta.bin")).revision
        session.replace_binary_file(
            _resolved(workdir, "meta.bin"), b"\x00new", rev, max_binary_bytes=8192
        )
        assert stat_module.S_IMODE(os.stat(target).st_mode) == 0o640
        assert target.read_bytes() == b"\x00new"


class TestSymlinkSecurity:
    """Symlinks fail closed exactly as the Linux backend reports them."""

    def test_final_symlink_refused(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "real.txt").write_text("x\n")
        os.symlink("real.txt", root / "link.txt")
        # stat reports the final symlink's type (it never followed it)
        st = session.stat(_resolved(workdir, "link.txt"))
        assert st.type == "symlink"
        # opening a final symlink for reading is refused by the kernel
        with pytest.raises(SymlinkNotAllowedError):
            session.read_text_page(
                _resolved(workdir, "link.txt"),
                start_line=1,
                max_lines=10,
                max_read_bytes=4096,
                binary_sample=64,
            )

    def test_parent_symlink_refused(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "realdir").mkdir()
        os.symlink("realdir", root / "dirlink")
        with pytest.raises(SymlinkNotAllowedError):
            session.stat(_resolved(workdir, "dirlink/f.txt"))
        with pytest.raises(SymlinkNotAllowedError):
            session.create_file(_resolved(workdir, "dirlink/f.txt"), "x\n", max_write_bytes=1024)

    def test_symlinked_directory_not_traversed_by_find(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "realdir").mkdir()
        (root / "realdir" / "target.txt").write_text("x")
        os.symlink("realdir", root / "dirlink")
        found, _ = session.find(
            _resolved(workdir, ""), pattern="*.txt", limit=10, max_walk_entries=1000
        )
        assert found == ["realdir/target.txt"]


class TestDarwinSearch:
    def test_literal_case_sensitivity_and_truncation(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "code.py").write_text("NEEDLE here\nplain\nNEEDLE again\n")
        (root / "other.txt").write_text("NEEDLE too\n")
        resolved = _resolved(workdir, "")
        matches, truncated = session.search(
            resolved,
            query="NEEDLE",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert not truncated
        assert [(m.path, m.line) for m in matches] == [
            ("code.py", 1),
            ("code.py", 3),
            ("other.txt", 1),
        ]
        insensitive, _ = session.search(
            resolved,
            query="needle",
            glob=None,
            case_sensitive=False,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert len(insensitive) == 3
        limited, truncated_limited = session.search(
            resolved,
            query="NEEDLE",
            glob=None,
            case_sensitive=True,
            limit=2,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert truncated_limited and len(limited) == 2

    def test_glob_filters(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "a.py").write_text("hit\n")
        (root / "b.txt").write_text("hit\n")
        (root / "sub").mkdir()
        (root / "sub" / "c.py").write_text("hit\n")
        resolved = _resolved(workdir, "")
        by_name, _ = session.search(
            resolved,
            query="hit",
            glob="*.py",
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert sorted(m.path for m in by_name) == ["a.py", "sub/c.py"]
        by_path, _ = session.search(
            resolved,
            query="hit",
            glob="sub/*.py",
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert [m.path for m in by_path] == ["sub/c.py"]

    def test_hidden_deny_vcs_reserved_never_surface(self, session, workdir) -> None:
        root = workdir.container_path
        (root / ".hidden.txt").write_text("secret\n")
        (root / "secret.pem").write_text("secret\n")
        vcs = root / ".git"
        vcs.mkdir()
        (vcs / "config").write_text("secret\n")
        (root / ".serverfs-tmp-abcdef0123456789").write_text("secret\n")
        resolved = _resolved(workdir, "")
        matches, _ = session.search(
            resolved,
            query="secret",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert matches == []
        # the same files DO surface when hidden is allowed and not denied
        from serverfs_mcp.paths import DenyPolicy

        open_resolved = resolve_workdir_path(
            workdir, "", allow_hidden=True, deny_policy=DenyPolicy(default_deny_enabled=False)
        )
        allowed, _ = session.search(
            open_resolved,
            query="secret",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        paths_found = sorted(m.path for m in allowed)
        # .git and the reserved temp name are never searched; .hidden.txt
        # and secret.pem are searchable with hidden+deny off
        assert paths_found == [".hidden.txt", "secret.pem"]

    def test_oversized_and_binary_files_skipped(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "big.txt").write_text("needle" + "x" * 4096)
        (root / "nul.bin").write_bytes(b"needle\x00needle")
        resolved = _resolved(workdir, "")
        matches, _ = session.search(
            resolved,
            query="needle",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=1024,
        )
        assert matches == []
        # big.txt matches under a bigger bound; nul.bin never does — the
        # NUL sits in the FIRST 64 KiB chunk, and rg suppresses a binary
        # file from the chunk that contains the first NUL onward (the
        # probe-verified Linux behavior: a needle before the NUL in one
        # small file still yields nothing)
        matches2, _ = session.search(
            resolved,
            query="needle",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=65536,
        )
        assert [m.path for m in matches2] == ["big.txt"]
        # a needle in the first chunk of a file whose first NUL sits in a
        # LATER chunk still matches (rg keeps reading until that chunk);
        # the file must stay under the size bound, so this uses 128 KiB
        (root / "late.bin").write_bytes(b"needle" + b"x" * 65530 + b"\x00tail")
        matches3, _ = session.search(
            resolved,
            query="needle",
            glob=None,
            case_sensitive=True,
            limit=10,
            timeout_seconds=5.0,
            max_file_bytes=131072,
        )
        assert sorted(m.path for m in matches3) == ["big.txt", "late.bin"]

    def test_deadline_enforced(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "x.txt").write_text("needle\n")
        resolved = _resolved(workdir, "")
        with pytest.raises(BackendError) as excinfo:
            session.search(
                resolved,
                query="needle",
                glob=None,
                case_sensitive=True,
                limit=10,
                timeout_seconds=0.0,
                max_file_bytes=65536,
            )
        assert excinfo.value.code == "SEARCH_TIMEOUT"

    def test_search_root_not_a_directory(self, session, workdir) -> None:
        root = workdir.container_path
        (root / "file.txt").write_text("x")
        with pytest.raises((NotADirectoryError, OSError)):
            session.validate_directory(_resolved(workdir, "file.txt"))
