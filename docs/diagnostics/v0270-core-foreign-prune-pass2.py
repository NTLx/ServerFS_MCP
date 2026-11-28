"""Prune only demonstrably unreferenced, byte-identical foreign Core copies.

An owning platform and original Git blob must still contain each candidate.
Conservative token scanning of retained local Python, shell, PowerShell, TOML,
YAML, native and Docker inputs includes comments and dynamic import strings.
Any reference blocks pruning, transitively. This is a cleanup proof, not a
substitute for native test execution or packaging verification.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-core-foreign-prune-pass2.json"
PLATFORMS = ("linux", "macos", "windows")
SRC = Path("src/serverfs_mcp")
EXTENSIONS = {".py", ".sh", ".ps1", ".cmd", ".bat", ".toml", ".yml", ".yaml",
              ".rs", ".swift", ".plist", ".json", ".service"}
SKIP = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "target",
        "dist", "build", ".git", ".remember", "node_modules"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def owner(path):
    name = path.parts[2] if len(path.parts) > 2 else ""
    if name.startswith("darwin_"):
        return "macos"
    if name.startswith("windows_") or name in {
        "agent_core_grant_client.py", "native_lifecycle.py"
    }:
        return "windows"
    if name.startswith("linux_"):
        return "linux"
    return None


def main():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == "feat/v0.27.0-platform-split"
    assert not OUT.exists()
    tracked = [
        Path(name) for name in git("ls-files", "-z", str(SRC)).decode().split("\0") if name
    ]
    entries = []
    blocked = {}
    for platform in PLATFORMS:
        base = ROOT / platform
        candidates = {}
        for rel in tracked:
            actual_owner = owner(rel)
            if actual_owner is None or actual_owner == platform or rel.suffix != ".py":
                continue
            path = base / rel
            retained = ROOT / actual_owner / rel
            if not path.is_file():
                continue
            # Do not remove independently changed or unmatched files.
            original = git("show", BASE + ":" + rel.as_posix())
            if (path.is_symlink() or not retained.is_file() or retained.is_symlink()
                or path.read_bytes() != original or retained.read_bytes() != original):
                blocked[path.relative_to(ROOT).as_posix()] = ["original or owner differs"]
                continue
            candidates[path] = (retained, hashlib.sha256(original).hexdigest())
        # Package names are referenced as a whole (including dynamic -m).
        names = {}
        for path in candidates:
            rel = path.relative_to(base / SRC)
            token = rel.parts[0] if len(rel.parts) > 1 else path.stem
            names.setdefault(token, set()).add(path)
        if not names:
            continue
        matcher = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(
            re.escape(name) for name in sorted(names)
        ) + r")(?![A-Za-z0-9_])")
        refs = {}
        for folder in ("src", "tests", "deployment", "agent_bridge", "native"):
            for path in (base / folder).rglob("*"):
                if not path.is_file() or path.is_symlink() or SKIP.intersection(path.relative_to(base).parts):
                    continue
                if path.suffix not in EXTENSIONS and path.name not in {"Dockerfile"}:
                    continue
                tokens = set(matcher.findall(path.read_text(encoding="utf-8", errors="replace")))
                for token in tokens:
                    for target in names[token]:
                        if target != path:
                            refs.setdefault(target, set()).add(path)
        # Include root-local build inputs.
        for path in base.iterdir():
            if path.is_file() and (path.suffix in EXTENSIONS or path.name == "Dockerfile"):
                for token in set(matcher.findall(path.read_text(encoding="utf-8", errors="replace"))):
                    for target in names[token]:
                        refs.setdefault(target, set()).add(path)
        pending = set(candidates)
        while True:
            retained_refs = {
                target: sorted(ref.relative_to(ROOT).as_posix() for ref in refs.get(target, ()) if ref not in pending)
                for target in pending
            }
            stop = {target for target, references in retained_refs.items() if references}
            if not stop:
                break
            for target in stop:
                blocked[target.relative_to(ROOT).as_posix()] = retained_refs[target]
            pending -= stop
        for path in sorted(pending):
            retained, digest = candidates[path]
            entries.append((path, retained, digest))
    # Check all bytes immediately before deleting any candidate.
    for path, retained, digest in entries:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == digest
    for path, _, _ in entries:
        path.unlink()
    result = {
        "baseline": BASE,
        "removed": [{"path": p.relative_to(ROOT).as_posix(),
                     "owner": o.relative_to(ROOT).as_posix(), "sha256": d} for p, o, d in entries],
        "blocked": blocked,
        "scope": "Exact foreign Core source copies only; conservative reference scan",
    }
    assert all(not p.exists() and o.exists() for p, o, _ in entries)
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print("PASS:", len(entries), "redundant foreign source files removed;",
          len(blocked), "retained because of references or owner mismatch")


if __name__ == "__main__":
    main()
