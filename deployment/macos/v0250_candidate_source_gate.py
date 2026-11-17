"""Read-only v0.25.0 macOS candidate inventory and opt-in isolated source snapshot.

This is a dirty-tree development gate, NOT an exact-SHA release or Git commit.
All candidate overlays are explicitly listed in a reviewed path manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

from serverfs_mcp.darwin_shared_release_source import materialize_release_source

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "deployment/macos/v0250_candidate_source_paths.txt"
SCRATCH_PREFIX = "deployment/macos/.serverfs-v0250-"
_ALLOWED_SUFFIXES = {".py", ".md", ".swift", ".sh", ".txt"}
_ALLOWED_RELEASE_METADATA = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "plugin.json",
        "agent_bridge/pyproject.toml",
        "agent_bridge/uv.lock",
    }
)
_ALLOWED_EXACT_PATHS = frozenset({".github/workflows/wheel-release.yml"})


class CandidateSourceError(ValueError):
    """A release input is not safe, reviewed or fully accounted for."""


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateSourceError("local Git inventory unavailable") from exc
    if result.returncode:
        raise CandidateSourceError("local Git inventory failed")
    return result.stdout


def _safe_path(value: str) -> None:
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or not parts
        or any(
            part in {"", ".", ".."} or (part.startswith(".") and value not in _ALLOWED_EXACT_PATHS)
            for part in parts
        )
        or "\\" in value
        or (
            path.suffix not in _ALLOWED_SUFFIXES
            and value not in _ALLOWED_RELEASE_METADATA
            and value not in _ALLOWED_EXACT_PATHS
        )
    ):
        raise CandidateSourceError("candidate manifest contains an unsafe path")


def read_manifest(path: Path) -> tuple[str, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    for entry in entries:
        _safe_path(entry)
    if not entries or entries != sorted(set(entries)):
        raise CandidateSourceError("candidate manifest must contain unique sorted paths")
    if any(entry.startswith(SCRATCH_PREFIX) for entry in entries):
        raise CandidateSourceError("candidate manifest includes generated scratch")
    return tuple(entries)


def dirty_paths(root: Path) -> tuple[set[str], int]:
    """Parse only simple unstaged modifications and untracked files; fail closed otherwise."""
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: set[str] = set()
    scratch = 0
    for item in raw.split(b"\0"):
        if not item:
            continue
        if len(item) < 4 or item[:2] not in {b" M", b"??"} or item[2:3] != b" ":
            raise CandidateSourceError("unexpected or staged Git change")
        try:
            name = item[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateSourceError("invalid Git filename encoding") from exc
        if name.startswith(SCRATCH_PREFIX):
            if item[:2] != b"??":
                raise CandidateSourceError("tracked generated scratch is unsafe")
            scratch += 1
            continue
        _safe_path(name)
        if name in paths:
            raise CandidateSourceError("duplicate Git status path")
        paths.add(name)
    return paths, scratch


def verify_candidate(root: Path, manifest: Path) -> tuple[tuple[str, ...], int, bytes]:
    root = root.resolve(strict=True)
    entries = read_manifest(manifest)
    observed, scratch = dirty_paths(root)
    if observed != set(entries):
        raise CandidateSourceError(
            f"candidate path mismatch: {len(set(entries) - observed)} stale, "
            f"{len(observed - set(entries))} unreviewed"
        )
    for entry in entries:
        src = root / entry
        try:
            current = src.lstat()
            canonical = src.resolve(strict=True)
        except OSError as exc:
            raise CandidateSourceError("candidate source path disappeared") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or canonical != src
            or not canonical.is_relative_to(root)
        ):
            raise CandidateSourceError("candidate file is not a confined regular file")
    return entries, scratch, _git(root, "rev-parse", "--verify", "HEAD").strip()


def snapshot_candidate(root: Path, manifest: Path, output: Path) -> tuple[int, int, str]:
    """Build a HEAD archive plus reviewed dirty-file overlays outside the live repo."""
    root = root.resolve(strict=True)
    output = output.absolute()
    within_private_scratch = (
        output.parent == root / "deployment/macos"
        and re.fullmatch(r"\.serverfs-v0250-candidate-[a-f0-9]{12}", output.name) is not None
    )
    if (
        (output.is_relative_to(root) and not within_private_scratch)
        or output.exists()
        or output.is_symlink()
    ):
        raise CandidateSourceError("snapshot requires a new outside path or named private scratch")
    if output.parent.resolve(strict=True) != output.parent:
        raise CandidateSourceError("snapshot parent must be canonical")
    entries, scratch, sha = verify_candidate(root, manifest)
    try:
        commit = sha.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CandidateSourceError("invalid repository commit encoding") from exc
    materialize_release_source(root, commit, output)
    for name in entries:
        src = root / name
        dest = output / name
        if dest.is_symlink() or dest.parent.resolve(strict=False) != dest.parent:
            raise CandidateSourceError("snapshot contains an unsafe overlay destination")
        content = src.read_bytes()
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(0o700 if src.stat().st_mode & stat.S_IXUSR else 0o600)
        if hashlib.sha256(dest.read_bytes()).digest() != hashlib.sha256(content).digest():
            raise CandidateSourceError("snapshot source byte parity failed")
    if dirty_paths(root)[0] != set(entries):
        raise CandidateSourceError("source inventory changed during snapshot")
    return len(entries), scratch, commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path, help="unused directory outside the repo")
    args = parser.parse_args(argv)
    try:
        if args.snapshot is None:
            entries, scratch, sha = verify_candidate(ROOT, args.manifest)
            print(
                f"CANDIDATE_INPUT=PASS reviewed={len(entries)} "
                f"excluded_scratch={scratch} head={sha.decode('ascii')}"
            )
        else:
            count, scratch, sha = snapshot_candidate(ROOT, args.manifest, args.snapshot)
            print(
                f"CANDIDATE_SNAPSHOT=PASS reviewed={count} "
                f"excluded_scratch={scratch} base_head={sha} "
                f"destination={args.snapshot}"
            )
        return 0
    except (CandidateSourceError, OSError, UnicodeError, ValueError) as exc:
        print(f"CANDIDATE_INPUT=FAIL {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
