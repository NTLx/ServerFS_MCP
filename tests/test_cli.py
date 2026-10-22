"""Tests for native CLI argument wiring and fail-closed behavior.

Focus: argument wiring, unsupported-platform refusal, config errors and
stdout/stderr discipline — doctor diagnostics never enter protocol stdout.
"""

from __future__ import annotations

import io
import sys
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


def _set_diagnostics_test_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if sys.platform == "win32":
        # Native diagnostics must use the SID-derived Named Pipe; a temporary socket path is
        # intentionally rejected by resolve_native_agent_wiring on Windows.
        monkeypatch.delenv("SERVERFS_AGENT_BRIDGE_SOCKET", raising=False)
    elif sys.platform == "darwin":
        # Keep the production-derived endpoint contract, but isolate its path from the user's
        # runtime directory so the test cannot touch a real Bridge socket.
        monkeypatch.delenv("SERVERFS_AGENT_BRIDGE_SOCKET", raising=False)
        monkeypatch.setattr(
            "serverfs_mcp.darwin_libc.darwin_bridge_socket_path",
            lambda: tmp_path / "bridge.sock",
        )
    else:
        monkeypatch.setenv("SERVERFS_AGENT_BRIDGE_SOCKET", str(tmp_path / "bridge.sock"))


class TestServe:
    def test_serve_refuses_on_non_windows(self, config_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("serverfs_mcp.cli.sys.platform", "linux")
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["serve", "--config", str(config_path)])
        assert code == 2
        assert "supported only on Windows" in err.getvalue()

    def test_serve_bad_config_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("serverfs_mcp.cli.sys.platform", "win32")
        bad = tmp_path / "bad.toml"
        bad.write_text('[[workdirs]]\nalias = "x"\npath = "relative"\n', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            with pytest.raises(SystemExit) as excinfo:
                main(["serve", "--config", str(bad)])
        assert excinfo.value.code == 2
        assert "configuration error" in err.getvalue()

    def test_serve_missing_config_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("serverfs_mcp.cli.sys.platform", "win32")
        err = io.StringIO()
        with redirect_stderr(err):
            with pytest.raises(SystemExit) as excinfo:
                main(["serve", "--config", str(tmp_path / "absent.toml")])
        assert excinfo.value.code == 2
        assert "not found" in err.getvalue()


class TestDoctor:
    @staticmethod
    def _config(tmp_path: Path) -> Path:
        ro = tmp_path / "ro"
        ro.mkdir()
        rw = tmp_path / "rw"
        rw.mkdir()
        config = tmp_path / "serverfs.toml"
        config.write_text(
            f'[[workdirs]]\nalias = "ro"\npath = "{ro.as_posix()}"\n'
            f'read_only = true\n\n[[workdirs]]\nalias = "rw"\n'
            f'path = "{rw.as_posix()}"\nread_only = false\n',
            encoding="utf-8",
        )
        return config

    def test_doctor_reports_full_probe_surface(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "data"))
        config = self._config(tmp_path)
        err = io.StringIO()
        out = io.StringIO()
        # stdout stays protocol-reserved: doctor must not print there
        with redirect_stderr(err), redirect_stdout(out):
            code = main(["doctor", "--config", str(config)])
        report = err.getvalue()
        assert out.getvalue() == ""
        assert code == 0, report
        assert "config: OK" in report
        assert "workdir: ro (read-only)" in report
        assert "workdir: rw (read-write)" in report
        assert "root: OK" in report
        assert "read: OK" in report
        assert "not evaluated (read-only workdir)" in report
        assert "add-file capability present" in report
        assert "tunnel-client" in report
        assert "proxy: disabled" in report
        assert "summary: 0 FAIL" in report

    def test_doctor_reports_invalid_config(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("not [ valid toml", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["doctor", "--config", str(bad)])
        assert code == 2
        assert "config: FAIL" in err.getvalue()

    def test_doctor_fails_on_missing_root(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "data"))
        config = tmp_path / "serverfs.toml"
        config.write_text(
            f'[[workdirs]]\nalias = "gone"\npath = "{(tmp_path / "missing").as_posix()}"\n',
            encoding="utf-8",
        )
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["doctor", "--config", str(config)])
        assert code == 1
        assert "root: FAIL" in err.getvalue()
        assert "does not exist" in err.getvalue()


class TestParser:
    def test_version_flag(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0

    def test_requires_command(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code != 0


class TestDiagnostics:
    def test_snapshot_json_is_stdout_only_and_unavailable_is_explicit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from serverfs_mcp.agent_client import AgentBridgeUnavailable

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def call(self, method, params):
                assert method == "diagnostics.snapshot"
                assert params == {}
                raise AgentBridgeUnavailable("offline")

        _set_diagnostics_test_endpoint(monkeypatch, tmp_path)
        monkeypatch.setattr("serverfs_mcp.agent_client.AgentBridgeClient", Client)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["diagnostics", "snapshot", "--format", "json"])
        assert code == 1
        assert out.getvalue() == '{"catalog_version":1,"disposition":"bridge_unavailable"}\n'
        assert err.getvalue() == ""

    def test_task_human_rendering_uses_returned_json_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        expected = {"catalog_version": 1, "disposition": "not_retained", "reason": "evicted"}

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def call(self, method, params):
                assert method == "diagnostics.task"
                assert params == {"task_id": "agt_local"}
                return expected

        _set_diagnostics_test_endpoint(monkeypatch, tmp_path)
        monkeypatch.setattr("serverfs_mcp.agent_client.AgentBridgeClient", Client)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["diagnostics", "task", "agt_local"])
        assert code == 0
        assert out.getvalue().splitlines() == [
            "catalog_version: 1",
            'disposition: "not_retained"',
            'reason: "evicted"',
        ]
        assert err.getvalue() == ""

    def test_available_human_output_preserves_completeness_and_negative_unaccounted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        projection = {
            "catalog_version": 1,
            "disposition": "available",
            "task_id": "agt_local",
            "completeness": "restarted_truncated_prefix",
            "rollups": {"unaccounted_seconds": -0.25},
        }

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def call(self, _method, _params):
                return projection

        _set_diagnostics_test_endpoint(monkeypatch, tmp_path)
        monkeypatch.setattr("serverfs_mcp.agent_client.AgentBridgeClient", Client)
        out = io.StringIO()
        with redirect_stdout(out):
            assert main(["diagnostics", "task", "agt_local"]) == 0
        rendered = out.getvalue()
        assert 'completeness: "restarted_truncated_prefix"' in rendered
        assert "rollups.unaccounted_seconds: -0.25" in rendered

    def test_requires_config(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["serve"])
        assert excinfo.value.code != 0
