"""Black-box parity: search_glob.py vs real ripgrep --glob (Linux gate).

The Windows searcher must accept/reject the same files ripgrep does on
Linux. Ripgrep is the ground truth, so this test runs wherever rg exists
(the Linux CI/container gate) and is skipped where it does not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from serverfs_mcp.search_glob import glob_matches

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")

TREE = [
    "a.txt",
    "b.md",
    "notes.TXT",
    "docs/readme.md",
    "docs/guide.txt",
    "docs/sub/deep.md",
    "docs/sub/extra.txt",
    "src/app.py",
    "src/pkg/mod.py",
    "src/a/b/c.py",
    "txtdir/inner",
    "foo/bar.txt",
    "x/foo/bar.txt",
    "?ap?.py",
    "z.txt",
]

PATTERNS = [
    "*.txt",
    "*.md",
    "*.py",
    "*",
    "?ap?.py",
    "[az].txt",
    "[!az].txt",
    "docs/*.md",
    "docs/*.txt",
    "docs/**",
    "**/*.txt",
    "**/deep.md",
    "src/**",
    "src/**/*.py",
    "foo/bar.txt",
    "**/bar.txt",
    "sub/extra.txt",
    "nope/*.txt",
    "*.TXT",
    "*.nomatch",
]


@pytest.fixture()
def rg_root(tmp_path: Path) -> Path:
    for rel in TREE:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"needle\n")
    return tmp_path


def _rg_matches(root: Path, pattern: str) -> set[str]:
    out = subprocess.run(
        ["rg", "--files", "--glob", pattern, "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode in (0, 1), out.stderr
    return {line.lstrip("./").removeprefix("./") for line in out.stdout.splitlines() if line}


@pytest.mark.parametrize("pattern", PATTERNS)
def test_glob_parity_with_ripgrep(rg_root: Path, pattern: str) -> None:
    rg_files = _rg_matches(rg_root, pattern)
    ours = {rel for rel in TREE if glob_matches(Path(rel).name, rel, pattern)}
    assert ours == rg_files, f"glob {pattern!r}: rg={sorted(rg_files)} ours={sorted(ours)}"


def test_glob_parity_relative_to_search_root(rg_root: Path, tmp_path: Path) -> None:
    # paths are relative to the SEARCH root, not the workdir root: run rg
    # from docs/ and compare against search-root-relative candidates
    rg_files = _rg_matches(rg_root / "docs", "*.md")
    sub = [rel.removeprefix("docs/") for rel in TREE if rel.startswith("docs/")]
    ours = {rel for rel in sub if glob_matches(Path(rel).name, rel, "*.md")}
    assert ours == rg_files, f"rg={sorted(rg_files)} ours={sorted(ours)}"
