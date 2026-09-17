"""search_text: rg-backed integration tests (requires ripgrep on PATH)."""

from __future__ import annotations

import shutil

import pytest

from serverfs_mcp.paths import resolve_workdir_path
from serverfs_mcp.search import run_search

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def resolve(workdir, path=""):
    return resolve_workdir_path(workdir, path, allow_hidden=False)


KW = "DATABASE_URL"


class TestLiteralQuery:
    @pytest.fixture()
    def tree(self, workdir) -> None:
        root = workdir.container_path
        (root / "src").mkdir()
        (root / "src" / "config.py").write_text(
            'import os\n\ndatabase_url = os.getenv("DATABASE_URL")\n'
        )
        (root / "src" / "util.py").write_text("nothing here\n")
        (root / "PandaWiki").mkdir()
        (root / "PandaWiki" / "docker-compose.yml").write_text(
            "services:\n  db:\n    env: DATABASE_URL=postgres://x\n"
        )

    def test_literal_match(self, workdir, tree) -> None:
        matches, truncated = run_search(
            resolve(workdir),
            query=KW,
            glob=None,
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        paths = [m.path for m in matches]
        assert "src/config.py" in paths
        assert "PandaWiki/docker-compose.yml" in paths
        assert truncated is False
        m = next(m for m in matches if m.path == "src/config.py")
        assert m.line == 3
        assert "DATABASE_URL" in m.text

    def test_special_regex_chars_literal(self, workdir) -> None:
        root = workdir.container_path
        (root / "r.txt").write_text("contains $(touch /tmp/pwned) literally\n")
        (root / "r.txt").write_text("a; rm -rf / b `command` c $(touch /tmp/pwned)\n")
        matches, _ = run_search(
            resolve(workdir),
            query="$(touch /tmp/pwned)",
            glob=None,
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert len(matches) == 1
        assert "$(touch /tmp/pwned)" in matches[0].text
        import os

        assert not os.path.exists("/tmp/pwned")

    def test_query_with_shell_metacharacters(self, workdir) -> None:
        root = workdir.container_path
        (root / "shell.txt").write_text("safe line\n; rm -rf /\n`command`\n$(touch x)\n")
        for q in ["; rm -rf /", "`command`", "$(touch x)"]:
            matches, _ = run_search(
                resolve(workdir),
                query=q,
                glob=None,
                case_sensitive=True,
                limit=50,
                timeout_seconds=15,
                max_file_bytes=52_428_800,
            )
            assert len(matches) == 1, f"query {q!r} should match once"


class TestCaseSensitivity:
    def test_case_sensitive(self, workdir) -> None:
        root = workdir.container_path
        (root / "cs.txt").write_text("DATABASE_URL\n database_url\n")
        matches, _ = run_search(
            resolve(workdir),
            query="DATABASE_URL",
            glob=None,
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert len(matches) == 1

    def test_case_insensitive(self, workdir) -> None:
        root = workdir.container_path
        (root / "ci.txt").write_text("DATABASE_URL\n database_url\n")
        matches, _ = run_search(
            resolve(workdir),
            query="database_url",
            glob=None,
            case_sensitive=False,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert len(matches) == 2


class TestGlob:
    def test_glob_filter(self, workdir) -> None:
        root = workdir.container_path
        (root / "a.py").write_text("DATABASE_URL\n")
        (root / "b.txt").write_text("DATABASE_URL\n")
        matches, _ = run_search(
            resolve(workdir),
            query="DATABASE_URL",
            glob="*.py",
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert [m.path for m in matches] == ["a.py"]


class TestChinese:
    def test_utf8_chinese_content(self, workdir) -> None:
        root = workdir.container_path
        (root / "zh.txt").write_text("第一行\n数据库连接串在配置里\n第三行\n")
        matches, _ = run_search(
            resolve(workdir),
            query="数据库连接串",
            glob=None,
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert len(matches) == 1
        assert matches[0].line == 2


class TestLimits:
    def test_result_limit(self, workdir) -> None:
        root = workdir.container_path
        (root / "lim.txt").write_text("NEEDLE\n" * 10)
        matches, truncated = run_search(
            resolve(workdir),
            query="NEEDLE",
            glob=None,
            case_sensitive=True,
            limit=3,
            timeout_seconds=15,
            max_file_bytes=52_428_800,
        )
        assert len(matches) == 3
        assert truncated is True

    def test_max_filesize_skips_big_files(self, workdir) -> None:
        root = workdir.container_path
        (root / "big.txt").write_text("NEEDLE\n" + "pad " * 100_000)
        (root / "small.txt").write_text("NEEDLE\n")
        matches, _ = run_search(
            resolve(workdir),
            query="NEEDLE",
            glob=None,
            case_sensitive=True,
            limit=50,
            timeout_seconds=15,
            max_file_bytes=100,
        )
        assert [m.path for m in matches] == ["small.txt"]

    def test_timeout(self, workdir) -> None:
        """Timeout enforcement: rg on a huge tree must be killed in time.

        rg is extremely fast (millions of lines/second), so we force real
        work: tens of thousands of files, each a few KiB. On any realistic
        machine this exceeds a 50 ms budget, so the subprocess timeout
        fires and SearchTimeout is raised. Also asserts no orphan rg.
        """
        import subprocess

        from serverfs_mcp.search import SearchTimeout

        root = workdir.container_path
        for i in range(20_000):
            (root / f"f{i:05d}.txt").write_text("NEEDLE filler\n" * 40)
        with pytest.raises(SearchTimeout):
            run_search(
                resolve(workdir),
                query="NEEDLE",
                glob=None,
                case_sensitive=True,
                limit=50,
                timeout_seconds=0.05,
                max_file_bytes=52_428_800,
            )
        # no orphaned rg process left behind
        out = subprocess.run(["pgrep", "-f", "rg --fixed-strings"], capture_output=True, text=True)
        assert "rg --fixed-strings" not in out.stdout
