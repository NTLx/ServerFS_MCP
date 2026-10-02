r"""Linux/Windows find+search contract parity (dev_plan §17 closing rule).

The SAME logical tree and the SAME assertions run against whichever
backend this process has: on Linux the rg-backed searcher is the
reference (its observable behaviour was measured, not guessed: bad UTF-8
lines never match, NUL suppression follows rg's 64 KiB buffer rule,
\r stays inside reported text, undecodable/oversized/VCS/symlink paths
are excluded exactly as measured); on Windows the native searcher must
produce the same agent-visible contract. Differences that are only
internal (walk order, chunking) are not asserted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import call_error, call_success, error_code
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry

NEEDLE = "NEEDLE"


def _seed(root: Path) -> None:
    (root / "docs" / "sub").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "big.txt").write_bytes(b"NEEDLE " + b"x" * 200 + b"\n")
    (root / "crlf.txt").write_bytes(b"foo NEEDLE\r\n")
    (root / "nonl.txt").write_bytes(b"NEEDLE tail-no-newline")
    (root / "badutf.txt").write_bytes(b"NEEDLE \xff tail\n")
    (root / "nul.txt").write_bytes(b"NEEDLE line one\nafter\x00NEEDLE line three\n")
    (root / "plain.txt").write_bytes(b"nothing here\n")
    (root / "docs" / "x.md").write_bytes(b"NEEDLE md\n")
    (root / "docs" / "sub" / "y.md").write_bytes(b"NEEDLE deep\n")
    (root / ".git" / "hidden.txt").write_bytes(b"NEEDLE in git\n")


def _server(root: Path, *, policy: EffectiveWorkdirPolicy | None = None, **settings_kw):
    wd = Workdir("t", root, None, read_only=True, policy=policy or EffectiveWorkdirPolicy())
    return create_server(Settings(**settings_kw), WorkdirRegistry([wd]))


EXPECTED_TREE_MATCHES = {
    ("big.txt", 1),
    ("crlf.txt", 1),
    ("nonl.txt", 1),
    ("docs/x.md", 1),
    ("docs/sub/y.md", 1),
}


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    wd_root = tmp_path / "repo"
    wd_root.mkdir()
    _seed(wd_root)
    return wd_root


def _matches(server, **args) -> set[tuple[str, int]]:
    result = call_success(server, "search_text", {"workdir": "t", **args})
    return {(m["path"], m["line"]) for m in result["matches"]}


class TestSearchContract:
    def test_tree_root_matches(self, root: Path) -> None:
        server = _server(root)
        assert _matches(server, query=NEEDLE, path="") == EXPECTED_TREE_MATCHES

    def test_crlf_text_keeps_carriage_return(self, root: Path) -> None:
        server = _server(root)
        result = call_success(server, "search_text", {"workdir": "t", "query": NEEDLE})
        crlf = next(m for m in result["matches"] if m["path"] == "crlf.txt")
        assert crlf["text"] == "foo NEEDLE\r"

    def test_no_final_newline_matches(self, root: Path) -> None:
        server = _server(root)
        result = call_success(server, "search_text", {"workdir": "t", "query": NEEDLE})
        nonl = next(m for m in result["matches"] if m["path"] == "nonl.txt")
        assert nonl["text"] == "NEEDLE tail-no-newline"

    def test_case_sensitivity(self, root: Path) -> None:
        server = _server(root)
        assert _matches(server, query="needle", path="") == set()
        ci = _matches(server, query="needle", path="", case_sensitive=False)
        assert ci == EXPECTED_TREE_MATCHES

    def test_glob_parity(self, root: Path) -> None:
        server = _server(root)
        assert _matches(server, query=NEEDLE, glob="docs/*.md") == {("docs/x.md", 1)}
        assert _matches(server, query=NEEDLE, glob="*.md") == {
            ("docs/x.md", 1),
            ("docs/sub/y.md", 1),
        }
        assert _matches(server, query=NEEDLE, glob="**/y.md") == {("docs/sub/y.md", 1)}
        assert _matches(server, query=NEEDLE, glob="docs/**") == {
            ("docs/x.md", 1),
            ("docs/sub/y.md", 1),
        }

    def test_nested_search_root_and_relative_glob(self, root: Path) -> None:
        server = _server(root)
        assert _matches(server, query=NEEDLE, path="docs") == {
            ("docs/x.md", 1),
            ("docs/sub/y.md", 1),
        }
        # glob is relative to the SEARCH root: *.md hits the deep file by
        # name, and docs/*.md from this root means docs/docs/*.md — nothing
        assert _matches(server, query=NEEDLE, path="docs", glob="sub/*.md") == {
            ("docs/sub/y.md", 1)
        }

    def test_file_as_root_is_not_a_directory(self, root: Path) -> None:
        server = _server(root)
        msg = call_error(
            server, "search_text", {"workdir": "t", "query": NEEDLE, "path": "big.txt"}
        )
        assert error_code(msg) == "NOT_A_DIRECTORY"

    def test_missing_root(self, root: Path) -> None:
        server = _server(root)
        msg = call_error(server, "search_text", {"workdir": "t", "query": NEEDLE, "path": "gone"})
        assert error_code(msg) == "PATH_NOT_FOUND"

    def test_size_ceiling_skips_only_big_file(self, root: Path) -> None:
        server = _server(root, search_max_file_bytes=50)
        assert _matches(server, query=NEEDLE, path="") == EXPECTED_TREE_MATCHES - {("big.txt", 1)}

    def test_limit_truncation(self, root: Path) -> None:
        server = _server(root)
        result = call_success(server, "search_text", {"workdir": "t", "query": NEEDLE, "limit": 2})
        assert result["returned"] == 2
        assert result["truncated"] is True

    def test_hidden_denied_and_vcs_never_surface(self, root: Path) -> None:
        server = _server(root)
        paths = {p for p, _ in _matches(server, query=NEEDLE, path="")}
        assert ".git/hidden.txt" not in paths
        assert "nul.txt" not in paths and "badutf.txt" not in paths

    def test_vcs_excluded_even_with_allow_hidden(self, root: Path) -> None:
        policy = EffectiveWorkdirPolicy(allow_hidden=True)
        server = _server(root, policy=policy, allow_hidden=True)
        assert ".git/hidden.txt" not in {p for p, _ in _matches(server, query=NEEDLE, path="")}


class TestFindContract:
    def test_find_name_pattern_hidden_off(self, root: Path) -> None:
        server = _server(root)
        result = call_success(server, "find_files", {"workdir": "t", "pattern": "*.txt"})
        paths = {m["path"] for m in result["matches"]}
        assert {
            "big.txt",
            "crlf.txt",
            "nonl.txt",
            "nul.txt",
            "badutf.txt",
            "plain.txt",
        } == paths

    def test_find_allow_hidden_surfaces_vcs(self, root: Path) -> None:
        # the rg VCS exclusions are a SEARCH mechanism; find_files has never
        # applied them (measured on the released Linux backend)
        policy = EffectiveWorkdirPolicy(allow_hidden=True)
        server = _server(root, policy=policy, allow_hidden=True)
        result = call_success(server, "find_files", {"workdir": "t", "pattern": "*"})
        paths = {m["path"] for m in result["matches"]}
        assert "docs/x.md" in paths and ".git/hidden.txt" in paths

    def test_find_limit_truncated(self, root: Path) -> None:
        server = _server(root)
        result = call_success(server, "find_files", {"workdir": "t", "pattern": "*", "limit": 1})
        assert result["returned"] == 1 and result["truncated"] is True

    def test_find_max_walk_truncated(self, root: Path) -> None:
        server = _server(root, max_walk_entries=1)
        result = call_success(server, "find_files", {"workdir": "t", "pattern": "*"})
        assert result["truncated"] is True

    def test_find_missing_root(self, root: Path) -> None:
        server = _server(root)
        msg = call_error(server, "find_files", {"workdir": "t", "pattern": "*", "path": "gone"})
        assert error_code(msg) == "PATH_NOT_FOUND"


def _dir_link(link: Path, target: Path) -> bool:
    if sys.platform == "win32":
        status = subprocess.run(
            ["cmd", "/C", "mklink", "/J", str(link), str(target)], capture_output=True
        ).returncode
        return status == 0
    os.symlink(target, link, target_is_directory=True)
    return True


class TestLinkContract:
    def test_directory_link_never_traversed(self, root: Path) -> None:
        if not _dir_link(root / "linkdir", root / "docs"):
            pytest.skip("could not create a directory link on this host")
        server = _server(root)
        assert _matches(server, query=NEEDLE, path="") == EXPECTED_TREE_MATCHES
        result = call_success(server, "find_files", {"workdir": "t", "pattern": "y.md"})
        assert {m["path"] for m in result["matches"]} == {"docs/sub/y.md"}

    def test_file_link_not_searched(self, root: Path) -> None:
        if sys.platform == "win32":
            try:
                os.symlink(root / "crlf.txt", root / "link.txt")
            except OSError:
                pytest.skip("file symlinks need Developer Mode here")
        else:
            os.symlink(root / "crlf.txt", root / "link.txt")
        server = _server(root)
        # the target crlf.txt matches once through its real name only
        assert _matches(server, query=NEEDLE, path="") == EXPECTED_TREE_MATCHES
