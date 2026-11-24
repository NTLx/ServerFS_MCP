"""Native loopback traffic proof for named Windows Agent proxy policy.

A synthetic HTTPS client exercises real sockets, not a provider SDK. No
external DNS, provider credentials, production services or tunnels are used.
"""

from __future__ import annotations

import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from serverfs_mcp.agent_lifecycle import bootstrap_frame_bytes, bridge_child_environment
from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_private_artifacts import protect_new_file
from serverfs_mcp.windows_v25 import storage
from serverfs_mcp.windows_v25.named_agent_proxy import load_named_agent_proxy

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows ACL proof")

_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_PYTHON = _ROOT / "agent_bridge" / ".venv" / "Scripts" / "python.exe"

_CLIENT = """
import socket
import sys
import urllib.error
import urllib.request

# Pin the synthetic HTTPS origin to our loopback listener; forbid external DNS.
resolve = socket.getaddrinfo
def local_only(host, *args, **kwargs):
    if host == "serverfs-proxy-fixture.invalid":
        host = "127.0.0.1"
    if host != "127.0.0.1":
        raise AssertionError("non-fixture DNS refused")
    return resolve(host, *args, **kwargs)
socket.getaddrinfo = local_only
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler(urllib.request.getproxies_environment())
)
try:
    opener.open("https://serverfs-proxy-fixture.invalid:" + sys.argv[1], timeout=3)
except (urllib.error.URLError, OSError):
    # Both observers deliberately terminate the connection before HTTPS success.
    print("PROBE_CLOSED")
else:
    raise AssertionError("fixture must not return an HTTPS response")
"""

_WORKER = """
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
from serverfs_agent_bridge.bootstrap import parse_bootstrap_frame
from serverfs_agent_bridge.runtime_proxy import build_runtime_environment

frame = parse_bootstrap_frame(sys.stdin.buffer.read())
# Adversarial ambient settings must not determine either child's route.
os.environ["HTTPS_PROXY"] = "http://ambient.invalid:1"
os.environ["ALL_PROXY"] = "http://ambient.invalid:2"
os.environ["NO_PROXY"] = "serverfs-proxy-fixture.invalid"
env = build_runtime_environment(
    os.environ, runtime="claude", use_proxy=sys.argv[2] == "on", proxy=frame.agent_proxy
)
child = subprocess.run(
    [sys.executable, "-I", "-B", "-c", sys.argv[3], sys.argv[4]],
    env=env, capture_output=True, timeout=8, check=False,
)
assert child.returncode == 0 and child.stdout.strip() == b"PROBE_CLOSED"
print("CHILD_PROBE_PASS")
"""


@contextmanager
def _observer(*, proxy: bool):
    observed: queue.Queue[bytes] = queue.Queue()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.connection.settimeout(4)
            if proxy:
                observed.put(self.rfile.readline(1024))
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            else:
                observed.put(self.rfile.read(1))

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], observed
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("route", ["proxy", "direct", "unavailable"])
def test_named_private_proxy_routes_real_loopback_traffic(tmp_path: Path, route: str) -> None:
    if not _BRIDGE_PYTHON.is_file():
        pytest.skip("separate local Bridge worker venv is not installed")
    sid = current_user_sid()
    with (
        _observer(proxy=True) as (proxy_port, proxy_events),
        _observer(proxy=False) as (origin_port, origin_events),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable,
    ):
        # Reserve an unreachable port without listening, avoiding port reuse.
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1] if route == "unavailable" else proxy_port
        source = tmp_path / "agent-proxy.env"
        source.write_text(f"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:{port}\n", encoding="utf-8")
        protect_new_file(source, sid)
        owner = subprocess.run(
            ["icacls.exe", str(source), "/setowner", f"*{sid}"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert owner.returncode == 0, "fixture owner setup failed"
        storage.check_private(source, directory=False, sid=sid)
        policy = {"claude": {"enabled": True, "use_proxy": True}}
        proxy = load_named_agent_proxy(bridge_home=tmp_path, sid=sid, bridge_document=policy)
        assert proxy is not None
        frame = bootstrap_frame_bytes(proxy, prior_bridge_execution_stopped=True)
        result = subprocess.run(
            [
                str(_BRIDGE_PYTHON),
                "-I",
                "-B",
                "-c",
                _WORKER,
                str(_ROOT / "agent_bridge" / "src"),
                "off" if route == "direct" else "on",
                _CLIENT,
                str(origin_port),
            ],
            input=frame,
            env=bridge_child_environment(proxy, source=os.environ),
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, "isolated proxy worker failed"
        assert result.stdout.strip() == b"CHILD_PROBE_PASS"
        if route == "proxy":
            assert proxy_events.get(timeout=3).startswith(
                f"CONNECT serverfs-proxy-fixture.invalid:{origin_port} ".encode()
            )
            assert origin_events.empty()
        elif route == "direct":
            assert origin_events.get(timeout=3) == b"\x16"  # TLS handshake
            assert proxy_events.empty()
        else:
            assert proxy_events.empty() and origin_events.empty(), "unexpected direct fallback"
