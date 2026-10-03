"""Phase F proxy-acceptance harness (WorkPC, real tunnel-client binary).

Executes the v0.10 proxy acceptance matrix locally by pointing the real
launcher chain at a loopback HTTPS control-plane stub and an inspectable
HTTP CONNECT proxy:

    serverfs tunnel -> tunnel-client --CONTROL_PLANE_BASE_URL=stub
                                         CONTROL_PLANE_HTTP_PROXY=proxy

The stub answers every request with 401, which is enough to prove the full
network path (proxy CONNECT -> TLS through the tunnel -> real HTTP request
arriving at the control plane) without touching the live OpenAI tunnel.
Scenario matrix (plan Phase F items): direct, unauthenticated proxy,
authenticated proxy with URL-reserved credentials (percent-encoding is
verified against what the upstream HTTP layer actually decodes), invalid
proxy credentials (407 -> redacted error), unreachable proxy, proxy that
drops an established connection, restart through the proxy, child-process
environment proof (no proxy or control-plane secrets), and proxy-disabled
behaving like the direct path.

Run on WorkPC:
    uv run --with cryptography --no-project -- \
        python deployment/native/proxy_acceptance_harness.py \
        --tunnel-client <bootstrapped tunnel-client.exe> \
        --serverfs-python <python with serverfs_mcp + native wheel>
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

RESERVED_USER = "acc3pt/ur+ser"
RESERVED_PASSWORD = "p@ss:w/o/rd?#1%"
TUNNEL_ID = "tunnel_" + "0" * 32


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def add(self, event: str, **fields: object) -> None:
        with self._lock:
            self.events.append({"event": event, "at": datetime.now(UTC).isoformat(), **fields})

    def count(self, event: str) -> int:
        with self._lock:
            return sum(1 for e in self.events if e["event"] == event)


REC = _Recorder()


class StubHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self._answer()

    def do_GET(self) -> None:  # noqa: N802
        self._answer()

    def _answer(self) -> None:
        REC.add("stub_request", method=self.command, path=self.path)
        body = b'{"error":{"type":"authentication_error","message":"acceptance stub"}}'
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default stderr logging
        return


class ProxyServer(socketserver.BaseRequestHandler):
    """HTTP CONNECT proxy. Class knobs configure auth and failure mode."""

    mode = "open"  # open | basic | refuse-upstream | drop-after-connect
    expect_user = ""
    expect_password = ""

    def handle(self) -> None:
        client = self.request
        # Read the CONNECT request header-bytes manually: once the CRLF CRLF
        # boundary arrives, any trailing bytes are already the beginning of
        # the TLS stream and must be forwarded verbatim (a line-oriented
        # buffered read would strand a ClientHello that contains no LF).
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, _, pending = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        verb, _, rest = lines[0].partition(" ")
        target = rest.split(" ", 1)[0]
        if verb != "CONNECT":
            REC.add("proxy_nonconnect", target=target)
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        auth_user = None
        auth_ok = True
        if self.mode == "basic":
            provided = headers.get("proxy-authorization", "")
            if provided.startswith("Basic "):
                decoded = base64.b64decode(provided[6:]).decode("latin-1", "replace")
                auth_user, _, auth_password = decoded.partition(":")
                auth_ok = auth_user == self.expect_user and auth_password == self.expect_password
            else:
                auth_ok = False
        REC.add(
            "proxy_connect",
            target=target,
            auth_user=auth_user,
            auth_ok=auth_ok,
            mode=self.mode,
        )
        if not auth_ok:
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="acceptance"\r\nContent-Length: 0\r\n\r\n'
            )
            return
        if self.mode == "drop-after-connect":
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            return  # closing immediately simulates an upstream drop
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=5)
        except OSError as exc:
            REC.add("proxy_upstream_failed", target=target, reason=type(exc).__name__)
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self._pump(client, upstream, pending)

    @staticmethod
    def _pump(client, upstream, pending: bytes) -> None:
        """Byte-exact bidirectional relay; no line framing on TLS streams."""

        def forward(source, sink, label: str, initial: bytes = b"") -> None:
            total = 0
            try:
                if initial:
                    sink.sendall(initial)
                    total += len(initial)
                while chunk := source.recv(65536):
                    sink.sendall(chunk)
                    total += len(chunk)
            except OSError:
                pass
            finally:
                REC.add("proxy_pump", direction=label, bytes=total)
                try:
                    upstream.close()
                finally:
                    client.close()

        down = threading.Thread(target=forward, args=(upstream, client, "down"), daemon=True)
        up = threading.Thread(target=forward, args=(client, upstream, "up", pending), daemon=True)
        down.start()
        up.start()
        down.join(timeout=120)
        up.join(timeout=120)


class ThreadingTCPServerReused(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ThreadingTLSServer(ThreadingTCPServerReused):
    def __init__(self, address, handler, context: ssl.SSLContext) -> None:
        super().__init__(address, handler)
        self.socket = context.wrap_socket(self.socket, server_side=True)


def make_stub_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "serverfs-acceptance-stub")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "stub.pem"
    key_path = directory / "stub-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def ip(text: str):
    import ipaddress

    return ipaddress.ip_address(text)


def serve(server: ThreadingTCPServerReused) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def run_launcher(
    *,
    repo_python: Path,
    tunnel_client: Path,
    config: Path,
    env_file: Path | None,
    stub_url: str,
    extra_env: dict[str, str],
) -> tuple[int | None, str]:
    argv = [
        str(repo_python),
        "-m",
        "serverfs_mcp.cli",
        "tunnel",
        "--config",
        str(config),
        "--tunnel-client",
        str(tunnel_client),
        "--tunnel-id",
        TUNNEL_ID,
        "--api-key-file",
        str(config.parent / "api-key"),
        "--base-url",
        stub_url,
    ]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    env = os.environ.copy()
    env.update(extra_env)
    # Popen + full-tree kill: subprocess.run's timeout would orphan the
    # tunnel-client grandchild, whose retry loops then keep hitting earlier
    # scenario proxies and corrupt the per-scenario counters.
    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = process.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill fallback failed
            process.kill()
            output, _ = process.communicate()
        return None, (output or "") + "\nTIMEOUT-KILLED-TREE"
    return process.returncode, output or ""


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.problems: list[str] = []

    def expect(self, condition: bool, detail: str) -> None:
        REC.add("check", name=self.name, ok=bool(condition), detail=detail)
        if not condition:
            self.problems.append(detail)

    @property
    def passed(self) -> bool:
        return not self.problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tunnel-client", required=True, type=Path)
    parser.add_argument("--serverfs-python", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--live-proxy",
        default=None,
        help="Real HTTP proxy host:port for the live control-plane scenario",
    )
    parser.add_argument(
        "--live-base-url",
        default="https://api.openai.com",
        help="Real control-plane endpoint used with --live-proxy",
    )
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="serverfs-proxy-acceptance-"))
    workdir = scratch / "wd"
    workdir.mkdir()
    cert_path, key_path = make_stub_cert(scratch)
    (scratch / "api-key").write_text("rtk_acceptance_stub_key_not_real\n", encoding="utf-8")
    config = scratch / "serverfs.toml"
    escaped_root = str(workdir).replace("\\", "\\\\")
    config.write_text(
        '[[workdirs]]\nalias = "harness"\npath = "' + escaped_root + '"\nread_only = true\n',
        encoding="utf-8",
    )

    stub_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    stub_context.load_cert_chain(cert_path, key_path)
    stub = ThreadingTLSServer(("127.0.0.1", 0), StubHandler, stub_context)
    stub_port = stub.server_address[1]
    serve(stub)
    stub_url = f"https://127.0.0.1:{stub_port}"

    def start_proxy(mode: str, user: str = "", password: str = "") -> int:
        handler = type(
            "BoundProxy",
            (ProxyServer,),
            {
                "mode": mode,
                "expect_user": user,
                "expect_password": password,
            },
        )
        proxy = ThreadingTCPServerReused(("127.0.0.1", 0), handler)
        serve(proxy)
        return proxy.server_address[1]

    unauth_port = start_proxy("open")
    auth_port = start_proxy("basic", RESERVED_USER, RESERVED_PASSWORD)
    drop_port = start_proxy("drop-after-connect")
    closed_port = unauth_port + 1
    while socket.socket().connect_ex(("127.0.0.1", closed_port)) == 0:
        closed_port += 1

    env_file_serial = 0

    def env_file_with(port: int, user: str = "", password: str = "") -> Path:
        nonlocal env_file_serial
        env_file_serial += 1
        path = scratch / f"proxy-env-{env_file_serial}.env"
        lines = ["SERVERFS_PROXY_HOST=127.0.0.1", f"SERVERFS_PROXY_PORT={port}"]
        if user:
            lines += [f"SERVERFS_PROXY_USERNAME={user}", f"SERVERFS_PROXY_PASSWORD={password}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    results: list[Check] = []

    # 1: direct connectivity (proxy configuration absent)
    check = Check("item1 direct")
    rc, _ = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=None,
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc} (401 retry loop until tree-kill expected)")
    check.expect(REC.count("stub_request") >= 1, "stub saw the control-plane request")
    check.expect(REC.count("proxy_connect") == 0, "no proxy was used")
    results.append(check)
    direct_stub_hits = REC.count("stub_request")

    # 2: HTTP proxy without authentication
    check = Check("item2 proxy no auth")
    rc, _ = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(unauth_port),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc}")
    check.expect(REC.count("proxy_connect") >= 1, "proxy saw a CONNECT")
    check.expect(
        REC.count("stub_request") >= direct_stub_hits + 1, "TLS reached the stub through the proxy"
    )
    results.append(check)
    stub_after_two = REC.count("stub_request")

    # 3: HTTP proxy with reserved-character credentials
    check = Check("item3 proxy auth + percent-encoding")
    rc, stderr = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(auth_port, RESERVED_USER, RESERVED_PASSWORD),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc}")
    authed = [e for e in REC.events if e["event"] == "proxy_connect" and e.get("auth_user")]
    check.expect(
        any(e.get("auth_user") == RESERVED_USER and e.get("auth_ok") for e in authed),
        f"proxy decoded reserved-char credentials exactly: {[e.get('auth_user') for e in authed]}",
    )
    check.expect(
        REC.count("stub_request") >= stub_after_two + 1, "authenticated proxy path completed"
    )
    check.expect(RESERVED_PASSWORD not in stderr, "launcher stderr carries no password")
    results.append(check)
    stub_after_three = REC.count("stub_request")

    # 4: invalid proxy credentials -> redacted 407 failure, stub never reached
    check = Check("item5 invalid proxy credentials")
    rc, stderr = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(auth_port, RESERVED_USER, "wrong-secret-pw"),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc}")
    check.expect(
        REC.count("stub_request") == stub_after_three, "stub was NOT reached (407 blocked)"
    )
    refused = [e for e in REC.events if e["event"] == "proxy_connect" and e.get("auth_ok") is False]
    check.expect(bool(refused), "proxy recorded an auth_ok=False CONNECT")
    check.expect("wrong-secret-pw" not in stderr, "stderr redacted the attempted password")
    results.append(check)

    # 5: unreachable proxy
    check = Check("item6 unreachable proxy")
    rc, stderr = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(closed_port),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc} (connection failure expected)")
    check.expect(REC.count("stub_request") == stub_after_three, "stub never reached")
    results.append(check)

    # 6: proxy drops an established connection
    check = Check("item7 established-connection drop")
    rc, _ = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(drop_port),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    dropped = [
        e
        for e in REC.events
        if e["event"] == "proxy_connect" and e.get("mode") == "drop-after-connect"
    ]
    check.expect(bool(dropped), "drop-mode proxy accepted then dropped the CONNECT")
    check.expect(rc != 0, f"rc={rc} (client survived the drop, then looped/was killed)")
    results.append(check)

    # 7: restart through the configured proxy (second successful proxy round-trip)
    check = Check("item8 restart through proxy")
    before = REC.count("proxy_connect")
    rc, _ = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=env_file_with(unauth_port),
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc != 0, f"rc={rc}")
    check.expect(REC.count("proxy_connect") >= before + 1, "re-run re-established through proxy")
    results.append(check)

    # 8: sanitized ServerFS child environment (real supervisor subprocess)
    check = Check("item9 child env without secrets")
    probe = scratch / "env_probe.py"
    probe.write_text(
        "import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))\n",
        encoding="utf-8",
    )
    dump = scratch / "child-env.json"
    dirty_env = os.environ.copy()
    dirty_env.update(
        {
            "CONTROL_PLANE_API_KEY": "rtk_live_key_must_not_leak",
            "CONTROL_PLANE_HTTP_PROXY": f"http://{RESERVED_USER}:{RESERVED_PASSWORD}@127.0.0.1:{auth_port}",
            "SERVERFS_PROXY_PASSWORD": "serverfs-proxy-secret-must-not-leak",
            "SERVERFS_PROXY_USERNAME": RESERVED_USER,
            "HTTPS_PROXY": "https://leak.example:1",
            "NO_PROXY": "leak.internal",
            "PATH": dirty_env.get("PATH", ""),
        }
    )
    completed = subprocess.run(
        [
            str(args.serverfs_python),
            "-c",
            "import sys; from serverfs_mcp.supervisor import forward_stdio;"
            f"forward_stdio([sys.executable, {str(probe)!r}, {str(dump)!r}])",
        ],
        env=dirty_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    child_env = json.loads(dump.read_text(encoding="utf-8")) if dump.exists() else {}
    joined = json.dumps(child_env)
    check.expect(completed.returncode == 0, f"probe ran (rc={completed.returncode})")
    check.expect(bool(child_env), "child environment captured")
    for forbidden in (
        "CONTROL_PLANE_API_KEY",
        "CONTROL_PLANE_HTTP_PROXY",
        "SERVERFS_PROXY_PASSWORD",
        "SERVERFS_PROXY_USERNAME",
        "HTTPS_PROXY",
        "NO_PROXY",
    ):
        check.expect(forbidden not in child_env, f"child env lacks {forbidden}")
    check.expect("rtk_live_key_must_not_leak" not in joined, "no control-plane key value")
    check.expect("serverfs-proxy-secret-must-not-leak" not in joined, "no proxy password value")
    check.expect(RESERVED_PASSWORD not in joined, "no credential-bearing URL")
    check.expect("PATH" in child_env, "PATH preserved for runtime resolution")
    results.append(check)

    # 9: proxy-disabled equals direct behavior (blank env file vs no file)
    check = Check("item10 proxy-disabled identical")
    connects_before = REC.count("proxy_connect")
    stubs_before = REC.count("stub_request")
    blank_env_file = scratch / "proxy-blank.env"
    blank_env_file.write_text("SERVERFS_PROXY_HOST=\nSERVERFS_PROXY_PORT=\n", encoding="utf-8")
    rc_blank, _ = run_launcher(
        repo_python=args.serverfs_python,
        tunnel_client=args.tunnel_client,
        config=config,
        env_file=blank_env_file,
        stub_url=stub_url,
        extra_env={"SSL_CERT_FILE": str(cert_path)},
    )
    check.expect(rc_blank != 0, f"rc={rc_blank}")
    check.expect(REC.count("proxy_connect") == connects_before, "blank proxy config used no proxy")
    check.expect(
        REC.count("stub_request") >= stubs_before + 1, "blank config still reached the stub"
    )
    results.append(check)

    # optional live scenario: real HTTP proxy against the real control plane
    # with a deliberately fake key. The expected outcome is the genuine
    # control-plane answer (401 Unauthorized / poll backoff) reached through
    # the operator's proxy: evidence of proxy+TLS+control-plane end to end
    # without consuming or touching any real tunnel.
    if args.live_proxy:
        check = Check("live real HTTP proxy -> real control plane")
        host, _, port = args.live_proxy.partition(":")
        live_env = scratch / "live-proxy.env"
        live_env.write_text(
            f"SERVERFS_PROXY_HOST={host}\nSERVERFS_PROXY_PORT={port}\n", encoding="utf-8"
        )
        rc, out = run_launcher(
            repo_python=args.serverfs_python,
            tunnel_client=args.tunnel_client,
            config=config,
            env_file=live_env,
            stub_url=args.live_base_url,
            extra_env={},
        )
        lowered = out.lower()
        check.expect(rc != 0, f"rc={rc}")
        check.expect(
            "401" in out or "unauthorized" in lowered or "poll failed" in lowered,
            "real control plane answered through the configured proxy",
        )
        check.expect("rtk_acceptance_stub_key_not_real" not in out, "key value never echoed")
        results.append(check)

    transcript = scratch / "transcript.json"
    transcript.write_text(json.dumps(REC.events, indent=1), encoding="utf-8")
    if args.out:
        Path(args.out).write_text(json.dumps(REC.events, indent=1), encoding="utf-8")

    failures = 0
    for check in results:
        status = "PASS" if check.passed else "FAIL"
        print(f"{status}  {check.name}")
        for problem in check.problems:
            failures += 1
            print(f"      - {problem}")
    print(f"transcript: {transcript}")
    print(
        f"proxy CONNECT: {REC.count('proxy_connect')}  stub requests: {REC.count('stub_request')}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
