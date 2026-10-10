"""Darwin native CLI / data-home tests (v0.13 Phase C)."""

from __future__ import annotations

import os
import sys

import pytest

from serverfs_mcp import cli
from serverfs_mcp.native_endpoint import BRIDGE_DIRECTORY, data_home

pytestmark = pytest.mark.skipif(
    not (sys.platform == "darwin" and os.uname().machine == "arm64"),
    reason="Darwin arm64 kernel contract",
)


class TestDataHome:
    def test_darwin_default_is_application_support(self, monkeypatch) -> None:
        import pathlib

        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        home = data_home(env={})
        assert home == pathlib.Path(
            os.path.expanduser("~/Library/Application Support/ServerFS")
        )
        assert home.name == "ServerFS"
        assert BRIDGE_DIRECTORY == "agent-bridge"

    def test_explicit_override_wins(self) -> None:
        import pathlib

        assert data_home(env={"SERVERFS_DATA_HOME": "/tmp/custom"}) == pathlib.Path(
            "/tmp/custom"
        )

    def test_tunnel_bootstrap_data_dir_matches(self, monkeypatch) -> None:
        from serverfs_mcp.tunnel_bootstrap import serverfs_data_dir

        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert str(serverfs_data_dir()).endswith("Library/Application Support/ServerFS")


class TestServeGate:
    @staticmethod
    def _run_capture(args: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                code = cli.main(args)
            except SystemExit as exc:  # config loader aborts this way
                code = exc.code if isinstance(exc.code, int) else 2
        return code, stderr.getvalue()

    def test_serve_runs_on_supported_darwin(self, tmp_path) -> None:
        """A supported darwin host gets past the platform gate.

        With a missing config the refusal is the config loader's (exit 2
        with a configuration message) -- NOT a platform refusal, which is
        what proves the gate passed.
        """
        code, message = self._run_capture(["serve", "--config", str(tmp_path / "absent.toml")])
        assert code == 2
        assert "configuration" in message
        assert "NATIVE_PLATFORM_UNSUPPORTED" not in message

    def test_serve_refusal_is_measured_not_label_based(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "serverfs_mcp.darwin_platform._rosetta_translated", lambda: True
        )
        code, message = self._run_capture(["serve", "--config", str(tmp_path / "absent.toml")])
        assert code == 2
        assert "Rosetta" in message


class TestBootstrap:
    def test_native_wheel_refused_on_macos(self, capsys) -> None:
        result = cli.main(["bootstrap", "native-wheel", "--url", "https://x/y.whl",
                           "--sha256", "0" * 64])
        assert result == 2
        err = capsys.readouterr().err
        assert "not required on macOS" in err
