"""Parse all candidate Python source/tests without importing foreign kernels."""
from __future__ import annotations
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/diagnostics/v0270-core-native-parse.json"
SKIP = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", "target", ".remember"}


def main():
    counts = {}
    for platform in ("linux", "macos", "windows"):
        base = ROOT / platform
        files = []
        for name in ("src", "tests", "agent_bridge/src", "agent_bridge/tests", "deployment"):
            for path in (base / name).rglob("*.py"):
                if SKIP.intersection(path.relative_to(base).parts):
                    continue
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                files.append(path)
        counts[platform] = len(files)
    with OUT.open("x", encoding="utf-8") as handle:
        json.dump({"syntax_parsed": counts, "scope": "Python 3.12 AST only; no native imports or runtime acceptance"}, handle, indent=2)
        handle.write("\n")
    print("PASS", counts)


if __name__ == "__main__":
    main()
