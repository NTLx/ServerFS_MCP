"""Resume reviewed Git checkpoint after three whitespace-only corrections.

Revalidate the exact staged scope and all original reviewed bytes, allow only
the three specified whitespace corrections, record new digests, and commit
the complete checkpoint without modifying any unrelated index or worktree path.
"""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
BRANCH = "feat/v0.27.0-platform-split"
REPORT = "docs/diagnostics/v0270-sync-checkpoint-review.json"
SELF = "docs/diagnostics/v0270-sync-checkpoint-resume.py"
FIXES = {
    "docs/diagnostics/v0270-phase0-baseline.md": "trailing",
    "macos/agent_bridge/src/serverfs_agent_bridge/protocol.py": "eof",
    "windows/agent_bridge/tests/test_codex_windows_runtime.py": "eof",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def names(*args):
    return {x.decode("utf-8") for x in git(*args).split(b"\0") if x}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    assert git("rev-parse", "HEAD").decode().strip() == BASE
    assert git("branch", "--show-current").decode().strip() == BRANCH
    report = json.loads((ROOT / REPORT).read_text(encoding="utf-8"))
    assert report == json.loads(git("show", ":" + REPORT))
    assert report["baseline"] == BASE and report["branch"] == BRANCH
    assert report["total_reviewed"] == 1514 and report["unexpected_untracked"] == 0
    assert report["sentinel_exact_baseline"] is True
    records = report["records"]
    paths = {row["path"] for row in records}
    assert len(paths) == len(records)
    assert names("diff", "--cached", "--name-only", "-z") == paths | {REPORT}
    assert names("diff", "--name-only", "-z") == set(FIXES)
    assert names("ls-files", "--others", "--exclude-standard", "-z") == {SELF}
    changes = []
    for row in records:
        rel = row["path"]
        path = ROOT / rel
        assert stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
        assert all(not p.is_symlink() for p in path.parents if p != ROOT)
        data = path.read_bytes()
        mode = "100755" if path.stat().st_mode & 0o111 else "100644"
        assert mode == row["mode"]
        if rel not in FIXES:
            assert sha(data) == row["sha256"], rel
            continue
        previous = git("show", ":" + rel)
        assert sha(previous) == row["sha256"]
        if FIXES[rel] == "trailing":
            old = b"No Git commit/push or production changes. \n"
            assert previous.count(old) == 1
            expected = previous.replace(old, old.rstrip(b" \n") + b"\n")
        else:
            assert previous.endswith(b"\n\n")
            expected = previous[:-1]
        assert data == expected, rel
        changes.append({"path": rel, "before": row["sha256"], "after": sha(data)})
        row["sha256"] = sha(data)
    own = ROOT / SELF
    assert stat.S_ISREG(own.lstat().st_mode) and not own.is_symlink()
    records.append({"path": SELF, "sha256": sha(own.read_bytes()),
                    "mode": "100644", "changed": True})
    records.sort(key=lambda row: row["path"])
    report["total_reviewed"] = len(records)
    report["counts"]["docs"] += 1
    report["whitespace_corrections"] = changes
    (ROOT / REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    subprocess.check_call(["git", "--literal-pathspecs", "add", "--",
                           *FIXES, REPORT, SELF], cwd=ROOT)
    assert names("diff", "--cached", "--name-only", "-z") == paths | {REPORT, SELF}
    assert not names("diff", "--name-only", "-z")
    subprocess.check_call(["git", "diff", "--cached", "--check"], cwd=ROOT)
    subprocess.check_call(["git", "commit", "-m",
                           "chore: checkpoint v0.27.0 platform split for native validation"], cwd=ROOT)
    assert git("rev-parse", "HEAD^").decode().strip() == BASE
    assert not names("diff", "--name-only", "-z")
    assert not names("diff", "--cached", "--name-only", "-z")
    assert not names("ls-files", "--others", "--exclude-standard", "-z")
    print("CHECKPOINT_COMMIT=" + git("rev-parse", "HEAD").decode().strip())


if __name__ == "__main__":
    main()
