r"""Real-import smoke for the Windows native extension module.

Runs the built ``serverfs_windows_native`` pyd (PYTHONPATH-provided in
development, installed wheel in CI/Phase E) against real NTFS. Skipped
everywhere the module or platform is absent.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native kernel")

n = pytest.importorskip(
    "serverfs_windows_native",
    reason="serverfs-windows-native wheel/pyd not installed",
)

REJECTED_ROOTS = [
    ("C:\\Projects\\", "trailing separator on a directory"),
    ("\\\\.\\PhysicalDrive0", "device namespace"),
    ("\\\\?\\GLOBALROOT\\Device", "global object namespace"),
    ("\\\\?\\UNC\\server\\share", "extended UNC"),
    ("\\\\server\\share", "UNC"),
    ("\\\\?\\Volume{60392b61-0000-0000-0000-100000000000}\\", "volume GUID namespace"),
    ("relative\\path", "relative path"),
    ("C:/forward", "forward slashes"),
    ("", "empty path"),
]


def test_open_workdir_retains_identity(tmp_path: Path) -> None:
    session = n.open_workdir(str(tmp_path), True)
    first = session.object_token()
    assert first.startswith("vol-") and ":file-" in first
    # retention: token comes from the held handle, re-read equals itself
    assert session.object_token() == first
    assert session.read_only is True


@pytest.mark.parametrize(("root", "why"), REJECTED_ROOTS)
def test_open_workdir_refuses_out_of_scope_roots(root: str, why: str) -> None:
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(root, True)
    assert excinfo.value.args[0] == "INVALID_ROOT", why


def test_missing_root_fails_not_found() -> None:
    missing = Path(tempfile.gettempdir()) / "serverfs_native_absent_dir_xyz"
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(str(missing), True)
    assert excinfo.value.args[0] == "PATH_NOT_FOUND"


def test_error_messages_carry_no_host_paths(tmp_path: Path) -> None:
    with pytest.raises(n.NativeSessionError) as excinfo:
        n.open_workdir(str(tmp_path) + "\\", True)
    message = str(excinfo.value)
    assert str(tmp_path) not in message


def test_source_transaction_reads_from_the_object_it_holds(tmp_path: Path) -> None:
    """dev_plan_v0.11 §15, C0 completion contract item 5: the read happens under the hold.

    The transform runs between the target validation and the publication. That an external writer
    is refused *right there* is the whole claim; a reader still gets in, because reads are never
    supposed to exclude an Agent turn.
    """
    session = n.open_workdir(str(tmp_path), False)
    revision = session.create_bytes(["a.txt"], b"hello\n")
    observed: dict[str, bytes] = {}

    def build(data: bytes, revision_before: str) -> bytes:
        assert data == b"hello\n"
        assert revision_before == revision
        with pytest.raises(PermissionError):
            os.open(str(tmp_path / "a.txt"), os.O_WRONLY)
        with open(tmp_path / "a.txt", "rb") as handle:
            assert handle.read() == b"hello\n"
        observed["data"] = data
        return b"edited by the transaction\n"

    published = session.replace_bytes_from_source(["a.txt"], revision, 4096, build)
    assert observed["data"] == b"hello\n"
    assert (tmp_path / "a.txt").read_bytes() == b"edited by the transaction\n"
    assert session.stat(["a.txt"])[3] == published
    # The hold is gone with the transaction, so a writer is admitted again.
    fd = os.open(str(tmp_path / "a.txt"), os.O_WRONLY)
    os.close(fd)


def test_source_transaction_validates_before_reading(tmp_path: Path) -> None:
    session = n.open_workdir(str(tmp_path), False)
    session.create_bytes(["a.txt"], b"payload")

    def never(data: bytes, revision_before: str) -> bytes:
        raise AssertionError("the source must not be read for a rejected target")

    with pytest.raises(n.NativeSessionError) as stale:
        session.replace_bytes_from_source(["a.txt"], "v1:0000000000000000", 4096, never)
    assert stale.value.args[0] == "REVISION_CONFLICT"

    (tmp_path / "sub").mkdir()
    current = session.stat(["sub"])[3]
    for token in ("v1:0000000000000000", current):
        with pytest.raises(n.NativeSessionError) as wrong_kind:
            session.replace_bytes_from_source(["sub"], token, 4096, never)
        assert wrong_kind.value.args[0] == "NOT_A_FILE", token

    revision = session.stat(["a.txt"])[3]
    with pytest.raises(n.NativeSessionError) as too_large:
        session.replace_bytes_from_source(["a.txt"], revision, 4, never)
    assert too_large.value.args[0] == "FILE_TOO_LARGE"
    assert (tmp_path / "a.txt").read_bytes() == b"payload"


def test_source_transaction_keeps_the_builders_exception(tmp_path: Path) -> None:
    """A text-edit refusal is the caller's condition and must not become a filesystem error."""
    session = n.open_workdir(str(tmp_path), False)
    revision = session.create_bytes(["a.txt"], b"payload")

    def build(data: bytes, revision_before: str) -> bytes:
        raise ValueError("TEXT_EDIT_NOT_FOUND")

    with pytest.raises(ValueError, match="TEXT_EDIT_NOT_FOUND"):
        session.replace_bytes_from_source(["a.txt"], revision, 4096, build)
    assert (tmp_path / "a.txt").read_bytes() == b"payload"
    assert [name for name in os.listdir(tmp_path)] == ["a.txt"]
