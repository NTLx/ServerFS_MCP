"""Strict content snapshots for quiescent, disposable Windows acceptance fixtures.

This is test infrastructure, not a security boundary for live/untrusted trees.
The caller must ensure fixture writers have stopped. No provider state is read.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def _entry_kind(info) -> str:
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("fixture contains a symlink or reparse point")
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    raise ValueError("fixture contains a nonregular entry")


def snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Snapshot all relative entry names, types and regular-file SHA-256 digests."""
    if _entry_kind(root.lstat()) != "directory":
        raise ValueError("fixture root must be a directory")
    entries: dict[str, tuple[str, str]] = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            kind = _entry_kind(info)
            relative = path.relative_to(root).as_posix()
            if kind == "directory":
                entries[relative] = (kind, "")
                visit(path)
            else:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = path.lstat()
                if _entry_kind(after) != kind or (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError("fixture changed while hashing")
                entries[relative] = (kind, digest)

    visit(root)
    return entries


def summarize_delta(
    before: dict[str, tuple[str, str]], after: dict[str, tuple[str, str]]
) -> dict[str, int | bool]:
    """Report only counts; .remember is classified, never excluded from the gate."""
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    modified = {name for name in before.keys() & after.keys() if before[name] != after[name]}
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "modified_count": len(modified),
        "remember_added": any(
            name == ".remember" or name.startswith(".remember/") for name in added
        ),
        "unchanged": not (added or removed or modified),
    }


def test_workdir_integrity_identical_and_same_name_content_change(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    marker = root / "marker.txt"
    marker.write_bytes(b"one")
    before = snapshot(root)
    assert summarize_delta(before, snapshot(root))["unchanged"] is True
    marker.write_bytes(b"two")
    delta = summarize_delta(before, snapshot(root))
    assert delta == {
        "added_count": 0,
        "removed_count": 0,
        "modified_count": 1,
        "remember_added": False,
        "unchanged": False,
    }


def test_workdir_integrity_nested_remember_is_not_ignored(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    marker = root / "marker.txt"
    marker.write_bytes(b"marker")
    before = snapshot(root)
    (root / ".remember").mkdir()
    (root / ".remember" / "synthetic.txt").write_bytes(b"synthetic")
    after = snapshot(root)
    assert before["marker.txt"] == after["marker.txt"]
    assert summarize_delta(before, after) == {
        "added_count": 2,
        "removed_count": 0,
        "modified_count": 0,
        "remember_added": True,
        "unchanged": False,
    }


def test_workdir_integrity_other_alias_change_and_removal(tmp_path):
    alpha = tmp_path / "Alpha"
    bravo = tmp_path / "Bravo"
    alpha.mkdir()
    bravo.mkdir()
    marker = bravo / "marker.txt"
    marker.write_bytes(b"marker")
    before_alpha, before_bravo = snapshot(alpha), snapshot(bravo)
    marker.unlink()
    assert summarize_delta(before_alpha, snapshot(alpha))["unchanged"] is True
    assert summarize_delta(before_bravo, snapshot(bravo))["removed_count"] == 1
    assert summarize_delta(before_bravo, snapshot(bravo))["unchanged"] is False


def test_workdir_integrity_rejects_reparse_metadata():
    from types import SimpleNamespace

    info = SimpleNamespace(
        st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    with pytest.raises(ValueError, match="reparse point"):
        _entry_kind(info)


def test_workdir_integrity_rejects_symlink(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("native symlink creation unavailable")
    with pytest.raises(ValueError, match="symlink or reparse point"):
        snapshot(root)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junction semantics")
def test_workdir_integrity_rejects_native_junction(tmp_path):
    root = tmp_path / "fixture"
    target = tmp_path / "target"
    root.mkdir()
    target.mkdir()
    marker = target / "marker.txt"
    marker.write_bytes(b"unchanged")
    link = root / "junction"
    result = subprocess.run(
        ["cmd", "/C", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("native junction creation unavailable")
    try:
        with pytest.raises(ValueError, match="symlink or reparse point"):
            snapshot(root)
        with pytest.raises(ValueError, match="symlink or reparse point"):
            snapshot(link)
    finally:
        link.rmdir()
    assert marker.read_bytes() == b"unchanged"
    assert not link.exists()
