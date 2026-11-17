"""Extract an exact Git commit into a new private macOS build input directory.

This does not use the mutable worktree as build input. It invokes local Git
only, never checks out, resets, stages, commits or touches remotes. Archive
entries are extracted with an explicit allowlist; tarfile.extractall is avoided.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from .darwin_shared_runtime import DarwinSharedRuntimeError

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_ARCHIVE = 96 * 1024 * 1024
_MAX_FILES = 20000
_MAX_MEMBER = 16 * 1024 * 1024


def _git(source: Path, *args: str, timeout: int = 30) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise DarwinSharedRuntimeError("local Git is required for exact-commit build")
    try:
        completed = subprocess.run(
            [git, "-C", str(source), *args],
            env={
                "HOME": str(Path.home()),
                "PATH": os.defpath,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise DarwinSharedRuntimeError("local Git exact-commit verification unavailable") from None
    if completed.returncode != 0:
        raise DarwinSharedRuntimeError("local Git exact-commit verification failed")
    return completed.stdout


def _expected_head(source: Path, commit: str) -> None:
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise DarwinSharedRuntimeError("release commit must be a full lowercase Git SHA")
    try:
        top = Path(os.fsdecode(_git(source, "rev-parse", "--show-toplevel")).strip()).resolve(
            strict=True
        )
        current = _git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        kind = _git(source, "cat-file", "-t", commit).decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise DarwinSharedRuntimeError("release source Git identity is invalid") from None
    if top != source or current != commit or kind != "commit":
        raise DarwinSharedRuntimeError("release commit does not match source checkout HEAD")


def _extract_archive(archive: Path, destination: Path) -> None:
    """Create-only extraction with no links, devices, path traversal or duplicates."""
    if destination.exists() or destination.is_symlink():
        raise DarwinSharedRuntimeError("release snapshot destination is already occupied")
    if archive.stat().st_size > _MAX_ARCHIVE:
        raise DarwinSharedRuntimeError("release archive exceeds maximum size")
    seen: set[str] = set()
    total = 0
    created_destination = False
    try:
        destination.mkdir(mode=0o700)
        created_destination = True
        with tarfile.open(archive, "r:") as stream:
            for i, member in enumerate(stream):
                if i >= _MAX_FILES or not (member.isdir() or member.isfile()):
                    raise DarwinSharedRuntimeError("release archive has unsupported entries")
                raw = member.name.rstrip("/")
                parts = PurePosixPath(raw).parts
                if (
                    not raw
                    or raw.startswith("/")
                    or "\\" in raw
                    or any(part in {"", ".", ".."} for part in raw.split("/"))
                    or raw in seen
                ):
                    raise DarwinSharedRuntimeError("release archive contains unsafe paths")
                seen.add(raw)
                target = destination.joinpath(*parts)
                if target.resolve(strict=False) != target:
                    raise DarwinSharedRuntimeError("release archive path is not canonical")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if member.isdir():
                    if target.is_symlink() or (target.exists() and not target.is_dir()):
                        raise DarwinSharedRuntimeError("release archive directory conflicts")
                    target.mkdir(exist_ok=True, mode=0o700)
                    target.chmod(0o700)
                else:
                    total += member.size
                    if member.size < 0 or member.size > _MAX_MEMBER or total > _MAX_ARCHIVE:
                        raise DarwinSharedRuntimeError("release archive member is too large")
                    f = stream.extractfile(member)
                    if f is None:
                        raise DarwinSharedRuntimeError("release archive regular file is unreadable")
                    with target.open("xb") as out:
                        remaining = member.size
                        while remaining:
                            block = f.read(min(65536, remaining))
                            if not block:
                                raise DarwinSharedRuntimeError("release archive was truncated")
                            out.write(block)
                            remaining -= len(block)
                    target.chmod(0o700 if member.mode & stat.S_IXUSR else 0o600)
    except (OSError, tarfile.TarError, ValueError, RuntimeError) as exc:
        if created_destination:
            shutil.rmtree(destination, ignore_errors=True)
        raise DarwinSharedRuntimeError("release snapshot extraction failed") from exc
    if not (destination / "pyproject.toml").is_file() or not (destination / "uv.lock").is_file():
        raise DarwinSharedRuntimeError("release snapshot lacks locked project metadata")


def materialize_release_source(source: Path, commit: str, target: Path) -> Path:
    """Use Git object data for the exact HEAD, never working-tree file contents."""
    source = Path(source).resolve(strict=True)
    _expected_head(source, commit)
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise DarwinSharedRuntimeError("release snapshot requires unused absolute destination")
    if target.parent.resolve(strict=True) != target.parent:
        raise DarwinSharedRuntimeError("release snapshot parent must be canonical")
    archive = target.with_name(target.name + ".tar")
    if archive.exists() or archive.is_symlink():
        raise DarwinSharedRuntimeError("release archive scratch name is already occupied")
    try:
        _git(source, "archive", "--format=tar", "--output", str(archive), commit, timeout=90)
        _extract_archive(archive, target)
        return target
    finally:
        archive.unlink(missing_ok=True)


def read_release_commit_file(source: Path, commit: str, filename: str) -> bytes:
    """Read project metadata from a Git commit, never from a mutable checkout."""
    if filename not in {"pyproject.toml", "uv.lock"}:
        raise DarwinSharedRuntimeError("release metadata filename is not allowed")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise DarwinSharedRuntimeError("release commit is invalid")
    return _git(Path(source), "show", f"{commit}:{filename}")
