"""Native file-ingress helper tests (v0.13 Phase H).

The security property under test: the MCP main process never fetches —
a separate helper process serves the fetch endpoint, and on macOS the
MCP↔helper hop is HTTP over a private AF_UNIX socket. These tests run a
REAL helper subprocess over a REAL unix socket (no mocking of the
transport); the remote-URL validation itself keeps its existing
coverage in test_file_ingress.py.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from serverfs_mcp.binary_payload import BinaryTransferError
from serverfs_mcp.file_ingress_client import FileIngressClient

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native helper contract")


@pytest.fixture()
def unix_helper(tmp_path: Path):
    """A real helper subprocess bound to a private AF_UNIX socket.

    Uses a short /tmp root: the helper enforces the sun_path budget, and
    pytest's default tmp_path is too long for a 104-byte Darwin socket
    address (the enforcement itself is asserted in test_darwin_seams).
    """
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sfs-ingress-"))
    runtime = root / "file-ingress-v1"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "ingress.sock"
    env = {
        **os.environ,
        "SERVERFS_FILE_INGRESS_ENABLED": "true",
        "SERVERFS_FILE_INGRESS_SOCKET": str(socket_path),
        "SERVERFS_FILE_INGRESS_ALLOWED_HOSTS": "files.example.test",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "serverfs_mcp.file_ingress"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(root / "helper.stderr.log", "wb"),
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            # the helper must actually be accepting: wait for a real listen
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.5)
                probe.connect(str(socket_path))
                probe.close()
                break
            except OSError:
                time.sleep(0.05)
                continue
        if process.poll() is not None:
            err = (root / "helper.stderr.log").read_bytes().decode(errors="replace")
            raise AssertionError(f"helper exited: {err[:400]}")
        time.sleep(0.05)
    else:
        process.kill()
        raise AssertionError("helper socket never appeared")
    # the runtime dir must stay private
    assert (runtime.stat().st_mode & 0o777) == 0o700

    class _Helper:
        pass

    helper = _Helper()
    helper.socket_path = socket_path
    helper.process = process
    yield helper
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    # the socket file is cleaned up by the helper
    assert not socket_path.exists()


class TestUnixHelperTransport:
    def test_helper_has_no_tcp_listener(self, unix_helper) -> None:
        # unix-socket mode must not open ANY TCP listening socket: ask the
        # kernel about THIS helper process rather than probing a fixed port
        # (an unrelated dev server may own 8081 on a developer machine)
        proc = subprocess.run(
            ["lsof", "-a", "-p", str(unix_helper.process.pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        listeners = [
            line for line in proc.stdout.splitlines() if line and not line.startswith("COMMAND")
        ]
        assert listeners == [], f"helper opened TCP listeners: {listeners}"

    def test_rejects_disallowed_host_through_unix_socket(self, unix_helper: Path) -> None:
        client = FileIngressClient(socket_path=unix_helper.socket_path)
        with pytest.raises(BinaryTransferError) as refused:
            client.fetch("https://evil.test/payload", max_bytes=1024)
        if refused.value.code == "FILE_INGRESS_UNAVAILABLE":
            helper_log = unix_helper.socket_path.parents[1] / "helper.stderr.log"
            detail = helper_log.read_text(errors="replace")[:400] if helper_log.exists() else ""
            pytest.fail(f"helper unreachable; stderr: {detail}")
        assert refused.value.code in {
            "FILE_INGRESS_URL_NOT_ALLOWED",
            "FILE_INGRESS_HOST_NOT_ALLOWED",
        }

    def test_transport_error_maps_to_ingress_unavailable(self, tmp_path: Path) -> None:
        # a socket path nobody serves: the client reports unavailability
        absent = tmp_path / "no-helper" / "ingress.sock"
        client = FileIngressClient(socket_path=absent)
        with pytest.raises(BinaryTransferError) as failed:
            client.fetch("https://files.example.test/x", max_bytes=1024)
        assert failed.value.code == "FILE_INGRESS_UNAVAILABLE"


class TestServeEnvOverlay:
    """cmd_serve overlays file-ingress env fields onto directly-built Settings."""

    def test_agent_enabled_settings_get_ingress_env(self, monkeypatch) -> None:
        from serverfs_mcp.cli import _overlay_ingress_env
        from serverfs_mcp.config import Settings

        monkeypatch.setenv("SERVERFS_FILE_INGRESS_ENABLED", "true")
        monkeypatch.setenv("SERVERFS_FILE_INGRESS_TIMEOUT_SECONDS", "45.0")
        settings = Settings(log_level="INFO", agent_bridge_enabled=True)
        assert settings.file_ingress_enabled is False  # direct construction ignores env

        overlaid = _overlay_ingress_env(settings)
        assert overlaid.file_ingress_enabled is True
        assert overlaid.file_ingress_timeout_seconds == 45.0
        # agent domain untouched
        assert overlaid.agent_bridge_enabled is True
        assert overlaid.log_level == "INFO"

    def test_env_off_keeps_disabled(self, monkeypatch) -> None:
        from serverfs_mcp.cli import _overlay_ingress_env
        from serverfs_mcp.config import Settings

        monkeypatch.delenv("SERVERFS_FILE_INGRESS_ENABLED", raising=False)
        settings = Settings(log_level="INFO")
        overlaid = _overlay_ingress_env(settings)
        assert overlaid.file_ingress_enabled is False
