"""Unit pins for the Windows search glob matcher (search_glob.py)."""

from __future__ import annotations

import pytest

from serverfs_mcp.search_glob import glob_matches

CASES: list[tuple[str, str, str, bool]] = [
    # separator-free pattern: file name at ANY depth
    ("*.txt", "a.txt", "a.txt", True),
    ("*.txt", "a.txt", "pkg/deep/a.txt", True),
    ("*.txt", "b.md", "pkg/b.md", False),
    ("*.txt", "a.TXT", "a.TXT", False),  # case-sensitive like rg on Linux
    ("*", "anything.rs", "x/y/anything.rs", True),
    ("?ap?.py", "xapy.py", "d/xapy.py", True),
    ("?ap?.py", "xappy.py", "xappy.py", False),
    ("[abc].txt", "b.txt", "d/b.txt", True),
    ("[!abc].txt", "b.txt", "b.txt", False),
    ("[!abc].txt", "z.txt", "z.txt", True),
    # pattern with /: search-root-relative path, components must align
    ("docs/*.md", "a.md", "docs/a.md", True),
    ("docs/*.md", "a.md", "docs/sub/a.md", False),  # fnmatch would over-match
    ("docs/*.md", "a.md", "other/a.md", False),
    ("**/*.txt", "t.txt", "t.txt", True),  # **/ matches zero directories
    ("**/*.txt", "t.txt", "x/y/t.txt", True),
    ("src/**", "mod.rs", "src/mod.rs", True),
    ("src/**", "mod.rs", "src/a/mod.rs", True),
    ("src/**", "a.rs", "other/a.rs", False),
    ("foo/bar.txt", "bar.txt", "foo/bar.txt", True),
    ("foo/bar.txt", "bar.txt", "x/foo/bar.txt", False),
    # unterminated [ is literal (fnmatch/rg convention)
    ("[ab.txt", "[ab.txt", "[ab.txt", True),
]


@pytest.mark.parametrize("glob,name,rel,expected", CASES)
def test_glob_matcher_table(glob: str, name: str, rel: str, expected: bool) -> None:
    assert glob_matches(name, rel, glob) is expected, f"{glob!r} vs {rel!r}"
