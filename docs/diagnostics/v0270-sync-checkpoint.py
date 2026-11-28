"""Review and prepare the explicitly approved Phase 1 Git synchronization checkpoint.

No staging until all checks pass. The exact seed allowlist comes from the reviewed
baseline; newly authored files and tracked edits are enumerated, not glob-added.
No production data, ignored files, arbitrary Git ref, hooks, or force push.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
BRANCH = "feat/v0.27.0-platform-split"
TRACKED = [
    ".github/workflows/container-publish.yml",
    ".github/workflows/container.yml",
    "deployment/registry/validate.py",
    "tests/test_registry_manifest.py",
    "tests/test_release_workflow.py"
]
DOCS = [
    "contracts/README.md",
    "contracts/v0.26.0/README.md",
    "contracts/v0.26.0/mcp-tools.json",
    "contracts/v0.27.0/acceptance-and-api-deltas.md",
    "docs/diagnostics/v0270-bridge-test-ownership.json",
    "docs/diagnostics/v0270-bridge-test-ownership.py",
    "docs/diagnostics/v0270-bridge-transport-audit.json",
    "docs/diagnostics/v0270-bridge-transport-audit.py",
    "docs/diagnostics/v0270-capture-v026-contract.py",
    "docs/diagnostics/v0270-core-foreign-prune-pass2.json",
    "docs/diagnostics/v0270-core-foreign-prune-pass2.py",
    "docs/diagnostics/v0270-core-foreign-prune.json",
    "docs/diagnostics/v0270-core-foreign-prune.py",
    "docs/diagnostics/v0270-core-native-parse.json",
    "docs/diagnostics/v0270-core-native-parse.py",
    "docs/diagnostics/v0270-distribution-test-ownership.json",
    "docs/diagnostics/v0270-distribution-test-ownership.py",
    "docs/diagnostics/v0270-inventory-v026.py",
    "docs/diagnostics/v0270-linux-candidate-artifacts.json",
    "docs/diagnostics/v0270-linux-candidate-artifacts.py",
    "docs/diagnostics/v0270-linux-wheel-build.json",
    "docs/diagnostics/v0270-linux-wheel-build.py",
    "docs/diagnostics/v0270-phase0-baseline.md",
    "docs/diagnostics/v0270-phase0-tracked-inventory.json",
    "docs/diagnostics/v0270-phase1-seed.json",
    "docs/diagnostics/v0270-platform-ownership.json",
    "docs/diagnostics/v0270-platform-ownership.py",
    "docs/diagnostics/v0270-platform-split-audit.json",
    "docs/diagnostics/v0270-platform-split-audit.py",
    "docs/diagnostics/v0270-seed-platforms.py",
    "docs/diagnostics/v0270-sync-checkpoint.py"
]
NEW = {
    "linux/agent_bridge/tests/test_platform_local_data_home.py",
    "linux/agent_bridge/tests/test_platform_local_inspect_state.py",
    "macos/agent_bridge/tests/test_platform_local_data_home.py",
    "windows/agent_bridge/tests/test_platform_local_data_home.py",
}
# A tracked, deliberately empty disabled-slot fixture, not local private state.
SENTINEL = "linux/.empty/.serverfs-disabled"
OUT = ROOT / "docs/diagnostics/v0270-sync-checkpoint-review.json"
MESSAGE = "chore: checkpoint v0.27.0 platform split for native validation"
SECRET = re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----|github_pat_[A-Za-z0-9_]{40,}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9_-]{40,}")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def names(*args):
    return {x.decode("utf-8") for x in git(*args).split(b"\0") if x}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def review():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == BRANCH
    assert not names("diff", "--cached", "--name-only", "-z")
    assert names("diff", "--name-only", "-z") == set(TRACKED), "unexpected tracked changes"
    seed = runpy.run_path(str(ROOT / "docs/diagnostics/v0270-seed-platforms.py"))
    expected = {}
    for platform, paths in seed["PLATFORMS"].items():
        expected.update({platform + "/" + rel: (mode, oid)
                         for rel, (mode, oid) in seed["expected_tree"](paths).items()})
    unseen = names("ls-files", "--others", "--exclude-standard", "-z")
    permitted = set(expected) | set(DOCS) | NEW | {OUT.relative_to(ROOT).as_posix()}
    unexpected = unseen - permitted
    if unexpected:
        # Do not reveal names or read contents of unexpected/private paths.
        raise SystemExit(f"STOP: {len(unexpected)} unexpected untracked paths")
    required = set(DOCS) | NEW | {SENTINEL}
    assert required <= unseen, "missing expected authored files or sentinel"
    assert digest((ROOT / "contracts/v0.26.0/mcp-tools.json").read_bytes()) == "e4aff42ebcf40a587912b9d0832a74e263dc5f2ee28322feee296639a96aea4d"
    records = []
    for rel in sorted((unseen | set(TRACKED)) - {OUT.relative_to(ROOT).as_posix()}):
        path = ROOT / rel
        assert not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode), "nonregular source"
        assert all(not parent.is_symlink() for parent in path.parents if parent != ROOT), "symlink parent"
        data = path.read_bytes()
        mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        prior = expected.get(rel)
        changed = prior is None or seed["blob_hash"](data) != prior[1]
        if prior:
            assert mode == prior[0], f"mode changed: {rel}"
        if changed and rel != "docs/diagnostics/v0270-sync-checkpoint.py":
            # Compare against baseline for existing content; a matched illustrative
            # fixture already present in the original is not a newly introduced key.
            old = b""
            if prior:
                old = git("cat-file", "blob", prior[1])
            elif rel in TRACKED:
                old = git("show", BASE + ":" + rel)
            if set(SECRET.findall(data)) - set(SECRET.findall(old)):
                raise SystemExit(f"STOP: possible new credential-shaped content in reviewed source {rel}")
        if rel == SENTINEL:
            assert prior and not changed and data == b"", "disabled-slot fixture differs"
        records.append({"path": rel, "sha256": digest(data), "mode": mode, "changed": changed})
    groups = {}
    for row in records:
        key = row["path"].split("/")[0]
        groups[key] = groups.get(key, 0) + 1
    report = {"baseline": BASE, "branch": BRANCH, "counts": groups,
              "total_reviewed": len(records), "sentinel_exact_baseline": True,
              "unexpected_untracked": 0, "records": records,
              "limitations": "Allowlist, byte digests, file types/modes and narrow credential-pattern screening, not a comprehensive secret scanner or Gate A."}
    return report


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "review":
        assert not OUT.exists()
        result = review()
        with OUT.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
        print(json.dumps({k: v for k, v in result.items() if k != "records"}, indent=2))
    elif mode == "commit":
        # Re-review all content immediately before staging. Exact byte inventory
        # must still match the separate read-only review report.
        recorded = json.loads(OUT.read_text(encoding="utf-8"))
        assert recorded == review(), "candidate changed since review"
        files = [row["path"] for row in recorded["records"]] + [OUT.relative_to(ROOT).as_posix()]
        assert len(files) == len(set(files))
        # Git's native staging is necessary: deterministic MCP commit is capped
        # at 64 individual files and the complete checkpoint must be atomic.
        subprocess.check_call(["git", "--literal-pathspecs", "add", "--", *files], cwd=ROOT)
        assert names("diff", "--cached", "--name-only", "-z") == set(files)
        subprocess.check_call(["git", "diff", "--cached", "--check"], cwd=ROOT)
        # Respect configured Git hooks. Stop if the commit fails.
        subprocess.check_call(["git", "commit", "-m", MESSAGE], cwd=ROOT)
        sha = git("rev-parse", "HEAD").decode().strip()
        assert git("rev-parse", "HEAD^").decode().strip() == BASE
        assert not names("diff", "--name-only", "-z")
        assert not names("diff", "--cached", "--name-only", "-z")
        assert not names("ls-files", "--others", "--exclude-standard", "-z")
        print("CHECKPOINT_COMMIT=" + sha)
    else:
        raise SystemExit("usage: review or commit")


if __name__ == "__main__":
    main()
