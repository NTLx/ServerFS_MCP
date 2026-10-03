"""Tests for the full Phase E2 doctor report.

The suite is platform-honest: the backend-dependent happy path runs where
the platform kernel is importable (Linux always; Windows only when the
native wheel is installed). Probe classification, proxy redaction and
tunnel-client discovery are exercised on every platform.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.doctor import run_doctor

needs_kernel = pytest.mark.skipif(
    sys.platform == "win32" and importlib.util.find_spec("serverfs_windows_native") is None,
    reason="Windows native kernel not installed in this environment",
)


def _config(tmp_path: Path, dirs: dict[str, tuple[Path, bool]]) -> Path:
    blocks = []
    for alias, (root, read_only) in dirs.items():
        blocks.append(
            f'[[workdirs]]\nalias = "{alias}"\npath = "{root.as_posix()}"\n'
            f"read_only = {'true' if read_only else 'false'}\n"
        )
    config = tmp_path / "serverfs.toml"
    config.write_text("\n".join(blocks), encoding="utf-8")
    return config


def _run(config: Path, **kwargs) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run_doctor(config, writer=lines.append, **kwargs)
    return code, lines


@pytest.fixture()
def isolated_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "data-home"
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(home))
    return home


class TestCoreReport:
    @needs_kernel
    def test_happy_paths_report_zero_fail(self, tmp_path: Path, isolated_data_home: Path) -> None:
        ro = tmp_path / "ro"
        rw = tmp_path / "rw"
        ro.mkdir()
        rw.mkdir()
        (ro / "file.txt").write_text("content", encoding="utf-8")
        code, lines = _run(_config(tmp_path, {"ro": (ro, True), "rw": (rw, False)}))
        text = "\n".join(lines)
        assert code == 0, text
        assert "root: OK" in text
        assert "filesystem:" in text
        assert "reparse: OK" in text
        assert "read: OK" in text
        assert "write: not evaluated (read-only workdir)" in text
        assert "add-file capability present" in text
        assert "summary: 0 FAIL" in text

    def test_root_listing_exposes_no_entry_names(self, tmp_path: Path) -> None:
        # The read line must report a count, never file names (logs discipline).
        root = tmp_path / "wd"
        root.mkdir()
        (root / "secret-named.txt").write_text("x", encoding="utf-8")
        code, lines = _run(
            _config(tmp_path, {"wd": (root, True)}),
            env_file=None,
        )
        joined = "\n".join(lines)
        assert "secret-named.txt" not in joined

    def test_missing_root_fails_exit_one(self, tmp_path: Path, isolated_data_home: Path) -> None:
        code, lines = _run(_config(tmp_path, {"gone": (tmp_path / "nope", True)}))
        assert code == 1
        assert any(line.startswith("root: FAIL") and "does not exist" in line for line in lines)

    def test_non_directory_root_fails(self, tmp_path: Path, isolated_data_home: Path) -> None:
        as_file = tmp_path / "plain.txt"
        as_file.write_text("x", encoding="utf-8")
        code, lines = _run(_config(tmp_path, {"bad": (as_file, True)}))
        assert code == 1
        assert "not a directory" in "\n".join(lines)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_symlinked_root_is_reported(self, tmp_path: Path, isolated_data_home: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        code, lines = _run(_config(tmp_path, {"link": (link, True)}))
        assert code == 1
        assert any("reparse: FAIL" in line and "symlink" in line for line in lines)

    @pytest.mark.skipif(sys.platform != "win32", reason="NTFS junction semantics")
    def test_junctioned_root_is_reported(self, tmp_path: Path, isolated_data_home: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        made = subprocess.run(
            ["cmd", "/C", "mklink", "/J", str(link), str(real)],
            capture_output=True,
            text=True,
        )
        if made.returncode != 0:
            pytest.skip("junction creation with mklink /J unavailable on this host")
        try:
            code, lines = _run(_config(tmp_path, {"link": (link, True)}))
            assert code == 1
            assert any("reparse: FAIL" in line and "junction" in line for line in lines)
        finally:
            link.rmdir()  # removes the junction itself, never the target


@needs_kernel
class TestTunnelProbe:
    def test_not_configured_is_informative_not_failure(
        self, tmp_path: Path, isolated_data_home: Path
    ) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        code, lines = _run(_config(tmp_path, {"wd": (wd, True)}))
        text = "\n".join(lines)
        assert "not configured" in text
        assert code == 0

    def test_missing_explicit_path_fails(self, tmp_path: Path, isolated_data_home: Path) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        code, lines = _run(
            _config(tmp_path, {"wd": (wd, True)}),
            tunnel_client=tmp_path / "absent.exe",
        )
        assert code == 1
        assert any(line.startswith("tunnel-client: FAIL") for line in lines)

    def test_bootstrapped_client_is_discovered_and_version_probed(
        self, tmp_path: Path, isolated_data_home: Path
    ) -> None:
        real = Path(os.environ.get("SERVERFS_TEST_TUNNEL_CLIENT", ""))
        if not real.is_file():
            pytest.skip("set SERVERFS_TEST_TUNNEL_CLIENT to a real tunnel-client binary")
        install = (
            isolated_data_home
            / "bin"
            / f"tunnel-client-v9.9.9-{'windows' if sys.platform == 'win32' else 'linux'}-amd64"
        )
        install.mkdir(parents=True)
        exe = install / ("tunnel-client.exe" if sys.platform == "win32" else "tunnel-client")
        shutil.copy(real, exe)
        wd = tmp_path / "wd"
        wd.mkdir()
        code, lines = _run(_config(tmp_path, {"wd": (wd, True)}))
        assert code == 0, "\n".join(lines)
        tunnel_line = next(line for line in lines if line.startswith("tunnel-client:"))
        assert tunnel_line.startswith("tunnel-client: OK")
        assert "9.9.9" not in tunnel_line  # version comes from the binary, not the directory
        assert "bootstrap directory" in tunnel_line


@needs_kernel
class TestProxyProbe:
    def test_absent_env_file_means_direct(self, tmp_path: Path, isolated_data_home: Path) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        code, lines = _run(_config(tmp_path, {"wd": (wd, True)}))
        assert code == 0
        assert any("proxy: disabled" in line for line in lines)

    def test_enabled_proxy_reports_host_port_auth_never_secrets(
        self, tmp_path: Path, isolated_data_home: Path
    ) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        env = tmp_path / ".env"
        env.write_text(
            "SERVERFS_PROXY_HOST=127.0.0.1\nSERVERFS_PROXY_PORT=9\n"
            "SERVERFS_PROXY_USERNAME=s3cretuser\nSERVERFS_PROXY_PASSWORD=s3cretpass\n",
            encoding="utf-8",
        )
        code, lines = _run(_config(tmp_path, {"wd": (wd, True)}), env_file=env)
        text = "\n".join(lines)
        assert "proxy: enabled http host=127.0.0.1 port=9 auth=configured" in text
        assert "s3cretuser" not in text
        assert "s3cretpass" not in text
        assert "http://" not in text
        # port 9 (discard) is closed on loopback: reachability must fail redacted
        assert code == 1
        reach = next(line for line in lines if "proxy reachability" in line)
        assert "FAIL" in reach
        assert "s3cret" not in reach

    def test_malformed_proxy_config_fails_redacted(
        self, tmp_path: Path, isolated_data_home: Path
    ) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        env = tmp_path / ".env"
        env.write_text(
            "SERVERFS_PROXY_PORT=8080\nSERVERFS_PROXY_PASSWORD=topsecretvalue\n",
            encoding="utf-8",
        )
        code, lines = _run(_config(tmp_path, {"wd": (wd, True)}), env_file=env)
        assert code == 1
        text = "\n".join(lines)
        assert "proxy: FAIL" in text
        assert "topsecretvalue" not in text

    def test_reachable_proxy_connects(self, tmp_path: Path, isolated_data_home: Path) -> None:
        wd = tmp_path / "wd"
        wd.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            env = tmp_path / ".env"
            env.write_text(
                f"SERVERFS_PROXY_HOST=127.0.0.1\nSERVERFS_PROXY_PORT={port}\n",
                encoding="utf-8",
            )
            code, lines = _run(_config(tmp_path, {"wd": (wd, True)}), env_file=env)
        assert code == 0
        assert any("TCP connect" in line and "OK" in line for line in lines)
