"""search_text: streaming rg --json search, early-stop, timeout, policy.

Real-rg integration tests plus deterministic fake-Popen tests for the
streaming/timeout/orphan machinery (per the review doc: do not build huge
trees just to trigger slow paths).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from serverfs_mcp.paths import DenyPolicy, resolve_workdir_path
from serverfs_mcp.search import SearchTimeout, run_search

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")

KW = "DATABASE_URL"


def _search(
    wd,
    path: str = "",
    query: str = KW,
    *,
    allow_hidden: bool = False,
    deny: DenyPolicy | None = None,
    glob: str | None = None,
    case_sensitive: bool = True,
    limit: int = 50,
    timeout: float = 15,
    max_file_bytes: int = 52_428_800,
):
    resolved = resolve_workdir_path(
        wd, path, allow_hidden=allow_hidden, deny_policy=deny or DenyPolicy()
    )
    root_fd = os.open(str(wd.container_path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return run_search(
            root_fd,
            resolved,
            query=query,
            glob=glob,
            case_sensitive=case_sensitive,
            limit=limit,
            timeout_seconds=timeout,
            max_file_bytes=max_file_bytes,
        )
    finally:
        os.close(root_fd)


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
        matches, truncated = _search(workdir)
        paths = [m.path for m in matches]
        assert "src/config.py" in paths
        assert "PandaWiki/docker-compose.yml" in paths
        assert truncated is False
        m = next(m for m in matches if m.path == "src/config.py")
        assert m.line == 3
        assert "DATABASE_URL" in m.text

    def test_special_regex_chars_literal(self, workdir) -> None:
        root = workdir.container_path
        (root / "r.txt").write_text("a; rm -rf / b `command` c $(touch /tmp/pwned)\n")
        matches, _ = _search(workdir, query="$(touch /tmp/pwned)")
        assert len(matches) == 1
        assert "$(touch /tmp/pwned)" in matches[0].text
        assert not os.path.exists("/tmp/pwned")

    def test_query_with_shell_metacharacters(self, workdir) -> None:
        root = workdir.container_path
        (root / "shell.txt").write_text("safe line\n; rm -rf /\n`command`\n$(touch x)\n")
        for q in ["; rm -rf /", "`command`", "$(touch x)"]:
            matches, _ = _search(workdir, query=q)
            assert len(matches) == 1, f"query {q!r} should match once"

    def test_filename_with_colon(self, workdir) -> None:
        root = workdir.container_path
        (root / "foo:bar.txt").write_text("NEEDLE in colon name\n")
        matches, _ = _search(workdir, query="NEEDLE")
        assert [m.path for m in matches] == ["foo:bar.txt"]
        assert "NEEDLE" in matches[0].text

    def test_no_follow_flags_never_passed(self, workdir) -> None:
        """rg must never be told to follow symlinks."""
        from serverfs_mcp import search as search_mod

        captured = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                r, w = os.pipe()
                os.close(w)
                self.stdout = os.fdopen(r, "rb", buffering=0)
                r2, w2 = os.pipe()
                os.close(w2)
                self.stderr = os.fdopen(r2, "rb", buffering=0)
                self.pid = 0
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        real_popen = subprocess.Popen
        search_mod.subprocess.Popen = FakePopen
        try:
            _search(workdir, query="x")
        finally:
            search_mod.subprocess.Popen = real_popen
        assert "-L" not in captured["args"]
        assert "--follow" not in captured["args"]
        assert captured["kwargs"]["shell"] is False
        assert captured["kwargs"]["cwd"].startswith("/proc/self/fd/")


class TestCaseSensitivity:
    def test_case_sensitive(self, workdir) -> None:
        (workdir.container_path / "cs.txt").write_text("DATABASE_URL\n database_url\n")
        matches, _ = _search(workdir, case_sensitive=True)
        assert len(matches) == 1

    def test_case_insensitive(self, workdir) -> None:
        (workdir.container_path / "ci.txt").write_text("DATABASE_URL\n database_url\n")
        matches, _ = _search(workdir, query="database_url", case_sensitive=False)
        assert len(matches) == 2


class TestGlob:
    def test_glob_filter(self, workdir) -> None:
        (workdir.container_path / "a.py").write_text("DATABASE_URL\n")
        (workdir.container_path / "b.txt").write_text("DATABASE_URL\n")
        matches, _ = _search(workdir, glob="*.py")
        assert [m.path for m in matches] == ["a.py"]


class TestChinese:
    def test_utf8_chinese_content(self, workdir) -> None:
        (workdir.container_path / "zh.txt").write_text("第一行\n数据库连接串在配置里\n第三行\n")
        matches, _ = _search(workdir, query="数据库连接串")
        assert len(matches) == 1
        assert matches[0].line == 2


class TestLimits:
    def test_exact_limit_not_truncated(self, workdir) -> None:
        """3 matches, limit=3, rg completes: truncated=false (§11)."""
        (workdir.container_path / "lim.txt").write_text("NEEDLE\n" * 3)
        matches, truncated = _search(workdir, query="NEEDLE", limit=3)
        assert len(matches) == 3
        assert truncated is False

    def test_global_early_stop(self, workdir) -> None:
        """10 matches, limit=3: stop rg early, truncated=true."""
        (workdir.container_path / "lim.txt").write_text("NEEDLE\n" * 10)
        matches, truncated = _search(workdir, query="NEEDLE", limit=3)
        assert len(matches) == 3
        assert truncated is True

    def test_max_filesize_skips_big_files(self, workdir) -> None:
        (workdir.container_path / "big.txt").write_text("NEEDLE\n" + "pad " * 100_000)
        (workdir.container_path / "small.txt").write_text("NEEDLE\n")
        matches, _ = _search(workdir, query="NEEDLE", max_file_bytes=100)
        assert [m.path for m in matches] == ["small.txt"]


class TestPolicyOnResults:
    def test_scoped_search_root_denied(self, workdir) -> None:
        """§5: search root itself inside a denied subtree → DENIED_PATH.

        (Enforced by the resolver before rg starts — reproduced here at the
        same layer the tool uses.)
        """
        (workdir.container_path / "internal").mkdir()
        (workdir.container_path / "internal" / "x.py").write_text("SECRET=1\n")
        from serverfs_mcp.paths import DeniedPathError

        with pytest.raises(DeniedPathError):
            resolve_workdir_path(
                workdir,
                "internal",
                allow_hidden=False,
                deny_policy=DenyPolicy(extra_globs=("internal/**",)),
            )

    def test_results_carry_full_relative_path(self, workdir) -> None:
        """§5.1: result paths must be workdir-relative (prefix restored),
        not rg-relative to the search root."""
        root = workdir.container_path
        (root / "foo").mkdir()
        (root / "foo" / "a.txt").write_text("NEEDLE\n")
        matches, _ = _search(workdir, path="foo", query="NEEDLE")
        assert [m.path for m in matches] == ["foo/a.txt"]

    def test_results_below_denied_dir_filtered(self, workdir) -> None:
        """Matches inside a denied subdirectory never surface (root search)."""
        root = workdir.container_path
        (root / "foo").mkdir()
        (root / "foo" / "a.txt").write_text("NEEDLE\n")
        (root / "foo" / "b").mkdir()
        (root / "foo" / "b" / "c.txt").write_text("NEEDLE\n")
        matches, _ = _search(workdir, query="NEEDLE", deny=DenyPolicy(extra_globs=("b/**",)))
        assert [m.path for m in matches] == ["foo/a.txt"]

    def test_hidden_results_filtered_when_hidden_disabled(self, workdir) -> None:
        (workdir.container_path / ".h").mkdir()
        (workdir.container_path / ".h" / "a.txt").write_text("NEEDLE\n")
        matches, _ = _search(workdir, allow_hidden=False)
        assert matches == []

    def test_hidden_results_visible_when_allowed(self, workdir) -> None:
        (workdir.container_path / ".h").mkdir()
        (workdir.container_path / ".h" / "a.txt").write_text("NEEDLE\n")
        matches, _ = _search(workdir, query="NEEDLE", allow_hidden=True)
        assert [m.path for m in matches] == [".h/a.txt"]


class TestStreamingFakeRg:
    """Deterministic fake-Popen tests for stream control machinery."""

    def _fake_popen_factory(self, payload: bytes, *, never_eof: bool = False):
        """Build a FakePopen class writing payload into stdout pipe."""

        class FakePopen:
            created = []

            def __init__(self, args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                r, w = os.pipe()
                os.set_blocking(w, False)
                written = 0
                while written < len(payload):
                    try:
                        written += os.write(w, payload[written:])
                    except BlockingIOError:
                        break
                if not never_eof:
                    os.close(w)
                else:
                    self._w = w
                self.stdout = os.fdopen(r, "rb", buffering=0)
                r2, w2 = os.pipe()
                os.close(w2)
                self.stderr = os.fdopen(r2, "rb", buffering=0)
                self.pid = -1
                self.returncode = 0
                self.terminated = False
                self.killed = False
                FakePopen.created.append(self)

            def poll(self):
                return 0 if self.terminated else None

            def wait(self, timeout=None):
                self.returncode = -15 if self.terminated else 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                if getattr(self, "_w", None) is not None:
                    try:
                        os.close(self._w)
                    except OSError:
                        pass

            def kill(self):
                self.killed = True

        return FakePopen

    @staticmethod
    def _match_line(path: str, text: str, line: int) -> bytes:
        return (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": path},
                        "lines": {"text": text},
                        "line_number": line,
                    },
                }
            )
            + "\n"
        ).encode()

    def test_early_stop_terminates_rg(self, workdir, monkeypatch) -> None:
        from serverfs_mcp import search as search_mod

        payload = b"".join(self._match_line(f"f{i:03d}.txt", "NEEDLE\n", 1) for i in range(200))
        Fake = self._fake_popen_factory(payload)
        monkeypatch.setattr(search_mod.subprocess, "Popen", Fake)
        matches, truncated = _search(workdir, limit=3)
        assert len(matches) == 3
        assert truncated is True
        proc = Fake.created[0]
        assert proc.terminated is True  # rg was actually stopped early

    def test_timeout_no_orphan(self, workdir, monkeypatch) -> None:
        from serverfs_mcp import search as search_mod

        # stdout pipe never reaches EOF → select blocks → deadline fires
        Fake = self._fake_popen_factory(b"", never_eof=True)
        monkeypatch.setattr(search_mod.subprocess, "Popen", Fake)
        with pytest.raises(SearchTimeout):
            _search(workdir, timeout=0.05)
        proc = Fake.created[0]
        assert proc.terminated is True
        # streams must be closed by the cleanup path
        assert proc.stdout.closed

    def test_stream_parsed_incrementally(self, workdir, monkeypatch) -> None:
        from serverfs_mcp import search as search_mod

        payload = (
            b'{"type":"begin","data":{"path":{"text":"a.txt"}}}\n'
            + self._match_line("a.txt", "NEEDLE\n", 1)
            + b'{"type":"end","data":{}}\n'
            + b'{"type":"summary","data":{}}\n'
        )
        Fake = self._fake_popen_factory(payload)
        monkeypatch.setattr(search_mod.subprocess, "Popen", Fake)
        matches, truncated = _search(workdir, limit=50)
        assert [m.path for m in matches] == ["a.txt"]
        assert truncated is False
