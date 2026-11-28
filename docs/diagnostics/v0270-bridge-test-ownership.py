"""One-off ownership migration of already duplicated platform-specific Bridge tests.

Each deletion is preflighted against the original Git blob and the unchanged
copy in its owning platform. Only explicit platform-specific test names and
prefixes are eligible; no runtime source, mixed tests, or root file is removed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-bridge-test-ownership.json"
PLATFORMS = ("linux", "macos", "windows")
REL = Path("agent_bridge/tests")
WINDOWS_TESTS = {
    "test_acp_windows_process.py", "test_codex_windows_runtime.py",
    "test_dangling_reparse.py", "test_inspect_state.py",
    "test_private_state_dangling_reparse.py", "test_render_config.py",
    "test_render_config_publication.py", "test_supervised_bridge.py",
    "test_repo_git_windows.py", "test_repo_ops_windows.py",
}
MACOS_TESTS = {
    "test_core_grants_macos_ipc.py", "test_darwin_seams.py",
    "test_macos_owned_config.py",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def owner(name):
    if name.startswith("test_windows_") or name in WINDOWS_TESTS:
        return "windows"
    if name in MACOS_TESTS:
        return "macos"
    if name.startswith(("test_linux_v0250_", "test_linux_v026_")):
        return "linux"
    return None


def main():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == "feat/v0.27.0-platform-split"
    assert not OUT.exists()
    actions = []
    for root_test in sorted((ROOT / REL).glob("test_*.py")):
        name = root_test.name
        platform = owner(name)
        if platform is None:
            continue
        reference = git("show", BASE + ":" + (REL / name).as_posix())
        actual_owner = ROOT / platform / REL / name
        assert actual_owner.is_file() and not actual_owner.is_symlink(), str(actual_owner)
        assert actual_owner.read_bytes() == reference, "owner differs: " + name
        for other in PLATFORMS:
            if other == platform:
                continue
            candidate = ROOT / other / REL / name
            if not candidate.exists():
                continue
            assert candidate.is_file() and not candidate.is_symlink()
            assert candidate.read_bytes() == reference, "foreign copy differs: " + str(candidate)
            actions.append((candidate, actual_owner, hashlib.sha256(reference).hexdigest()))
    # Preflight all deletions before changing any file.
    for candidate, actual_owner, digest in actions:
        assert hashlib.sha256(candidate.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(actual_owner.read_bytes()).hexdigest() == digest
    for candidate, _, _ in actions:
        candidate.unlink()
    result = {
        "baseline": BASE,
        "deleted_duplicates": [
            {"removed": p.relative_to(ROOT).as_posix(),
             "retained_owner": o.relative_to(ROOT).as_posix(),
             "sha256": h} for p, o, h in actions
        ],
    }
    assert all(not p.exists() and o.exists() for p, o, _ in actions)
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print("PASS:", len(actions), "duplicate tests removed; exact owning copies retained")


if __name__ == "__main__":
    main()
