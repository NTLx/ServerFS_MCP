"""Tests for the serverfs CLI skeleton (v0.10 Phase A, §26).

Focus: argument wiring, fail-closed config errors, the Phase A serve
refusal, and the stdout/stderr discipline (§27) — doctor diagnostics go to
stderr so stdout remains protocol-clean for the future stdio transport.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from serverfs_mcp.cli import main

CONFIG = """\
[[workdirs]]
alias = "projects"
path = "/srv/projects"
read_only = false

[[workdirs]]
alias = "documents"
path = "/srv/documents"
"""


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(CONFIG, encoding="utf-8")
    return config


class TestServe:
    def test_serve_refuses_in_phase_a(self, config_path: Path) -> None:
        # Phase A must not silently serve the Linux streamable-HTTP topology
        # under a native config: that would be an undocumented deployment.
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["serve", "--config", str(config_path)])
        assert code == 2
        assert "not available in Phase A" in err.getvalue()

    def test_serve_bad_config_fails_closed(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[[workdirs]]\nalias = "x"\npath = "relative"\n', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            with pytest.raises(SystemExit) as excinfo:
                main(["serve", "--config", str(bad)])
        assert excinfo.value.code == 2
        assert "configuration error" in err.getvalue()

    def test_serve_missing_config_fails_closed(self, tmp_path: Path) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            with pytest.raises(SystemExit) as excinfo:
                main(["serve", "--config", str(tmp_path / "absent.toml")])
        assert excinfo.value.code == 2
        assert "not found" in err.getvalue()


class TestDoctor:
    def test_doctor_reports_config_and_workdirs(self, config_path: Path) -> None:
        err = io.StringIO()
        out = io.StringIO()
        # stdout stays protocol-reserved: doctor must not print there
        with redirect_stderr(err), redirect_stdout(out):
            code = main(["doctor", "--config", str(config_path)])
        assert code == 0
        report = err.getvalue()
        assert out.getvalue() == ""
        assert "config: OK" in report
        assert "workdir: projects (read-write)" in report
        assert "workdir: documents (read-only)" in report

    def test_doctor_reports_invalid_config(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("not [ valid toml", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["doctor", "--config", str(bad)])
        assert code == 2
        assert "config: FAIL" in err.getvalue()

    def test_doctor_names_phase_a_limits(self, config_path: Path) -> None:
        # The report must be honest about what was NOT checked (§ "Not
        # verified" discipline): root probes are not implemented in Phase A.
        err = io.StringIO()
        with redirect_stderr(err):
            main(["doctor", "--config", str(config_path)])
        assert "root probes: not implemented" in err.getvalue()


class TestParser:
    def test_version_flag(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0

    def test_requires_command(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code != 0

    def test_requires_config(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["serve"])
        assert excinfo.value.code != 0
