"""Phase 1 verified ownership cleanup: explicit foreign Bridge modules and Core tests.

All candidate deletions are preflighted against fixed Git blobs and retained
owner copies. Root remains intact. Direct imports by retained local tests or
Bridge modules block cleanup instead of silently breaking collection.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-platform-ownership.json"
PLATFORMS = ("linux", "macos", "windows")
BRIDGE = Path("agent_bridge/src/serverfs_agent_bridge")
SOURCES = {
    "windows": [BRIDGE / name for name in (
        "windows_ipc.py", "windows_pipe.py", "windows_lease.py",
    )] + [Path("agent_bridge/tests/pipe_support.py")],
    "linux": [BRIDGE / name for name in (
        "linux_v0250_install.py", "linux_v0250_preflight.py", "linux_v0250_provision.py",
    )],
    "macos": [BRIDGE / "macos_owned_config.py"],
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def test_owner(name):
    if name.startswith("test_windows_") or name in {
        "test_native_windows.py", "test_runtime_policy_roundtrip.py"
    }:
        return "windows"
    if name.startswith(("test_darwin_", "test_macos_")) or name.endswith("_macos.py"):
        return "macos"
    if name.startswith("test_linux_"):
        return "linux"
    return None


def imported_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                names.update(item.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(item.name for item in node.names)
    return names


def main():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == "feat/v0.27.0-platform-split"
    assert not OUT.exists()
    ownership = {rel: owner for owner, paths in SOURCES.items() for rel in paths}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        owner = test_owner(path.name)
        if owner:
            ownership[path.relative_to(ROOT)] = owner
    actions = {}
    for rel, owner in ownership.items():
        original = git("show", BASE + ":" + rel.as_posix())
        retained = ROOT / owner / rel
        assert retained.is_file() and not retained.is_symlink()
        assert retained.read_bytes() == original, "owner differs: " + str(rel)
        for platform in PLATFORMS:
            if platform == owner:
                continue
            candidate = ROOT / platform / rel
            if not candidate.exists():
                continue
            assert candidate.is_file() and not candidate.is_symlink()
            assert candidate.read_bytes() == original, "foreign copy differs: " + str(candidate)
            actions[candidate] = (retained, hashlib.sha256(original).hexdigest())
    # The old platform_seams facade has no users after native lease/IPC migration.
    for platform in PLATFORMS:
        candidate = ROOT / platform / BRIDGE / "platform_seams.py"
        original = git("show", BASE + ":" + (BRIDGE / "platform_seams.py").as_posix())
        assert candidate.is_file() and candidate.read_bytes() == original
        actions[candidate] = (ROOT / BRIDGE / "platform_seams.py", hashlib.sha256(original).hexdigest())

    # Refuse if a retained test or Bridge source imports a candidate's module.
    # Foreign Core production modules are still pending coordinated CLI pruning.
    blocked = []
    for platform in PLATFORMS:
        base = ROOT / platform
        selected = {p.stem: p for p in actions if p.is_relative_to(base)}
        for group in ("tests", "agent_bridge/tests", "agent_bridge/src"):
            for path in (base / group).rglob("*.py"):
                if path in actions:
                    continue
                for name in sorted(imported_names(path) & selected.keys()):
                    blocked.append((path.relative_to(ROOT).as_posix(), name))
    if blocked:
        raise SystemExit("retained local imports block deletion: " + json.dumps(blocked))

    for path, (retained, digest) in actions.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == digest
    for path in actions:
        path.unlink()
    result = {
        "baseline": BASE,
        "deleted": [
            {"removed": path.relative_to(ROOT).as_posix(),
             "retained": retained.relative_to(ROOT).as_posix(),
             "sha256": digest}
            for path, (retained, digest) in sorted(actions.items())
        ],
        "limitations": "Direct import scan only; Core CLI/native caller pruning and actual platform tests remain required.",
    }
    assert all(not path.exists() and retained.exists() for path, (retained, _) in actions.items())
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print("PASS:", len(actions), "verified redundant source/test files removed")


if __name__ == "__main__":
    main()
