"""Capture tracked-path and build/reference inventory before the v0.27.0 split.

One-off evidence only: read Git objects at the frozen baseline, never private state.
Classification is deliberately conservative; no path is approved for removal.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "fd1019498b281cdeb8c5805242f1a71222f5eebd"
OUT = ROOT / "docs/diagnostics/v0270-phase0-tracked-inventory.json"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def group(path):
    if path.startswith(("src/", "agent_bridge/src/")):
        return "runtime"
    if path.startswith(("tests/", "agent_bridge/tests/")):
        return "tests"
    if path.startswith(("deployment/", "native/")):
        return "deployment_native"
    if path.startswith(".github/"):
        return "ci"
    if path.startswith(("docs/", "site/", "skills/")):
        return "product_docs_distribution"
    if path.startswith("agent_bridge/"):
        return "bridge_build"
    return "root_or_other"


def hint(path):
    s = path.lower()
    hints = []
    for platform, pattern in (
        ("linux", r"(?:^|[/_.-])linux(?:[/_.-]|$)|(?:^|[/_.-])posix(?:[/_.-]|$)"),
        ("macos", r"(?:^|[/_.-])(?:macos|darwin)(?:[/_.-]|$)"),
        ("windows", r"(?:^|[/_.-])(?:windows|win32|ntfs)(?:[/_.-]|$)"),
    ):
        if re.search(pattern, s):
            hints.append(platform)
    return hints


def main():
    if git("rev-parse", "HEAD").decode().strip() != BASE:
        raise SystemExit("unexpected HEAD")
    raw = git("ls-tree", "-r", "-z", BASE)
    records = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, path = item.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        path = path.decode("utf-8")
        records.append({
            "path": path, "mode": mode, "kind": kind, "object": oid,
            "group": group(path), "platform_name_hints": hint(path),
        })
    # Reference scan of baseline text only; review all matches before relocation.
    # A lexical hit or filename hint is not proof of runtime import/ownership.
    patterns = [
        "src/serverfs_mcp", "agent_bridge/", "native/macos/", "native/windows/",
        "deployment/", "pyproject.toml", "uv.lock", "compose.yml",
        "tests/", "server.json", "plugin.json",
    ]
    references = {}
    for pattern in patterns:
        proc = subprocess.run(
            ["git", "grep", "-l", "-F", pattern, BASE, "--",
             ".github", "deployment", "native", "src", "agent_bridge",
             "tests", "site", "skills", "README.md", "README_EN.md",
             "pyproject.toml", "Dockerfile", "docs/maintainers"],
            cwd=ROOT, capture_output=True, check=False,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        references[pattern] = [
            line.removeprefix(BASE + ":")
            for line in proc.stdout.decode("utf-8").splitlines()
        ]
    schema = ROOT / "contracts/v0.26.0/mcp-tools.json"
    data = schema.read_bytes()
    parsed = json.loads(data)
    assert parsed["source_sha"] == BASE and parsed["mcp_sdk_version"] == "2.3.0"
    assert len(parsed["definitions"]) == 28 and len(parsed["variants"]) == 12
    counts = collections.Counter(r["group"] for r in records)
    result = {
        "baseline": BASE,
        "schema_sha256": hashlib.sha256(data).hexdigest(),
        "schema_bytes": len(data),
        "tracked_count": len(records),
        "groups": dict(sorted(counts.items())),
        "reference_hits": {k: len(v) for k, v in references.items()},
        "reference_paths": references,
        "records": records,
        "limitations": "Git-object and lexical inventory; hints not deletion decisions. Import closure, build and platform-local test ownership still require review.",
    }
    with OUT.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({k: result[k] for k in (
        "tracked_count", "groups", "reference_hits", "schema_sha256", "schema_bytes"
    )}, indent=2))


if __name__ == "__main__":
    main()
