"""One-time v0.27.0 Phase 1 seed from the exact reviewed v0.26.0 Git tree.

Creates independent *candidate* source subtrees only, preserving original bytes.
No runtime code generation or shared source copy at build time. No deletions,
Git staging, deployment, production state, or untracked source inputs.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-phase1-seed.json"
COMMON = (
    "src", "agent_bridge", "tests", "pyproject.toml", "uv.lock",
    "LICENSE", "README.md", "README_EN.md", ".gitignore", ".gitattributes",
    "serverfs.toml.example", "deployment/conformance_target.py",
    "deployment/native/proxy_acceptance_harness.py",
)
PLATFORMS = {
    "linux": COMMON + (
        "Dockerfile", ".dockerignore", ".empty", ".env.example",
        "compose.yml", "compose.agent.yml", "compose.cloudflare.yml",
        "compose.http.yml", "compose.oauth.yml", "deployment/linux",
        "deployment/agent-bridge", "deployment/tunnel",
    ),
    "macos": COMMON + ("native/macos", "deployment/macos"),
    "windows": COMMON + ("native/windows", "deployment/windows"),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def expected_tree(paths: tuple[str, ...]) -> dict:
    rows = {}
    for raw in git("ls-tree", "-r", "-z", BASE, "--", *paths).split(b"\0"):
        if not raw:
            continue
        meta, path_bytes = raw.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        path = path_bytes.decode("utf-8")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"unexpected tracked object: {path}")
        rows[path] = (mode, oid)
    return rows


def stage_one(platform: str, paths: tuple[str, ...], target: Path) -> dict:
    expected = expected_tree(paths)
    raw = git("archive", "--format=tar", BASE, "--", *paths)
    found = set()
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("unsafe archive member")
            if member.isdir():
                continue
            name = path.as_posix()
            if not member.isfile() or name not in expected or name in found:
                raise RuntimeError(f"unexpected archive member: {name}")
            entry = archive.extractfile(member)
            if entry is None:
                raise RuntimeError(f"missing blob: {name}")
            data = entry.read()
            mode, oid = expected[name]
            if blob_hash(data) != oid:
                raise RuntimeError(f"archive content mismatch: {name}")
            dst = target.joinpath(*path.parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            with dst.open("xb") as handle:
                handle.write(data)
            dst.chmod(0o755 if mode == "100755" else 0o644)
            found.add(name)
            total_bytes += len(data)
    if found != set(expected):
        raise RuntimeError(f"missing archive members for {platform}")
    # Read every staged file back, not merely trust the archive.
    for name, (mode, oid) in expected.items():
        dst = target.joinpath(*PurePosixPath(name).parts)
        if dst.is_symlink() or blob_hash(dst.read_bytes()) != oid:
            raise RuntimeError(f"staged content mismatch: {platform}/{name}")
        if bool(dst.stat().st_mode & 0o111) != (mode == "100755"):
            raise RuntimeError(f"staged mode mismatch: {platform}/{name}")
    return {"files": len(found), "bytes": total_bytes, "source_blob_checks": len(found)}


def main() -> None:
    if git("rev-parse", "HEAD").decode().strip() != BASE:
        raise SystemExit("unexpected HEAD")
    if git("branch", "--show-current").decode().strip() != "feat/v0.27.0-platform-split":
        raise SystemExit("unexpected branch")
    if git("diff", "--name-only") or git("diff", "--cached", "--name-only"):
        raise SystemExit("tracked changes present")
    for name in (*PLATFORMS, str(OUT.relative_to(ROOT))):
        if os.path.lexists(ROOT / name):
            raise SystemExit(f"target already exists: {name}")
    inventory = json.loads(
        (ROOT / "docs/diagnostics/v0270-phase0-tracked-inventory.json").read_text(encoding="utf-8")
    )
    if inventory["baseline"] != BASE or inventory["tracked_count"] != 1242:
        raise SystemExit("unexpected inventory")
    if hashlib.sha256((ROOT / "contracts/v0.26.0/mcp-tools.json").read_bytes()).hexdigest() != inventory["schema_sha256"]:
        raise SystemExit("schema snapshot changed")
    with tempfile.TemporaryDirectory(prefix=".v0270-seed-", dir=ROOT) as temp:
        scratch = Path(temp)
        result = {}
        for name, paths in PLATFORMS.items():
            result[name] = stage_one(name, paths, scratch / name)
        # All trees verified before publishing any directory.
        for name in PLATFORMS:
            if os.path.lexists(ROOT / name):
                raise RuntimeError(f"target appeared during seed: {name}")
        for name in PLATFORMS:
            (scratch / name).rename(ROOT / name)
    report = {
        "baseline": BASE, "platforms": result,
        "status": "seeded only; build, pruning, independent runtime and Gate A not verified",
        "root_preserved": True,
        "limitations": "Common baseline source/test modules temporarily duplicated within each candidate. Platform-specific native assets selected. Root packaging, CI, distribution and remaining test/import ownership must be migrated. Linux/Windows manifest macOS force-include is not yet removed.",
    }
    with OUT.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
