"""Move copied product-level test ownership back to the repository root.

Only exact duplicate copies are removed. Shared distribution inputs and
historical evidence remain at the root. Native test suites keep their local
fixtures and their platform-specific acceptance tests.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-distribution-test-ownership.json"
SHARED = (
    "tests/test_agent_skills.py",
    "tests/test_registry_manifest.py",
    "tests/test_release_metadata.py",
    "tests/test_release_workflow.py",
    "tests/test_wheel_release.py",
    "tests/e2e/test_v026_site_static_acceptance.py",
    "tests/e2e/v026_site_static_acceptance.py",
)
WINDOWS = (
    "tests/e2e/test_v026_windows_retention_policy.py",
    "tests/e2e/windows_lifecycle_retention.py",
)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == "feat/v0.27.0-platform-split"
    assert not OUT.exists()
    actions = []
    for platform in ("linux", "macos", "windows"):
        for rel in SHARED + (WINDOWS if platform != "windows" else ()):
            path = ROOT / platform / rel
            retained = ROOT / rel
            original = git("show", BASE + ":" + rel)
            assert path.is_file() and not path.is_symlink(), str(path)
            assert retained.is_file() and not retained.is_symlink(), str(retained)
            assert path.read_bytes() == retained.read_bytes() == original, str(path)
            if rel in WINDOWS:
                assert (ROOT / "windows" / rel).read_bytes() == original
            actions.append((path, retained, hashlib.sha256(original).hexdigest()))
    for path, retained, digest in actions:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == digest
    for path, _, _ in actions:
        path.unlink()
    assert all(not p.exists() and o.exists() for p, o, _ in actions)
    result = {
        "baseline": BASE,
        "removed": [{"path": p.relative_to(ROOT).as_posix(),
                     "retained": o.relative_to(ROOT).as_posix(),
                     "sha256": d} for p, o, d in actions],
        "scope": "Shared product distribution tests and site audit remain root-owned; Windows retention policy remains in Windows and root.",
    }
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print("PASS:", len(actions), "verified duplicate tests/helpers removed")


if __name__ == "__main__":
    main()
