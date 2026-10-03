"""Native .env/proxy handling and tunnel-client command construction."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from serverfs_mcp import native_tunnel
from serverfs_mcp.native_tunnel import (
    NativeTunnelError,
    encode_tunnel_command_argv,
    parse_proxy_env_file,
    proxy_url,
    run_native_tunnel,
)
from serverfs_mcp.supervisor import _copy, forward_stdio, sanitized_environment


def _env_file(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(contents, encoding="utf-8")
    return path


def test_proxy_parser_disabled_and_unknown_keys(tmp_path: Path) -> None:
    assert parse_proxy_env_file(_env_file(tmp_path, "OTHER=value\n# comment\n")) == {}
    assert proxy_url({}) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=0080\n",
            "http://proxy.internal:80",
        ),
        (
            'SERVERFS_PROXY_HOST="proxy.internal"\nSERVERFS_PROXY_PORT=7890\n',
            "http://proxy.internal:7890",
        ),
    ],
)
def test_proxy_parser_no_auth(tmp_path: Path, text: str, expected: str) -> None:
    assert proxy_url(parse_proxy_env_file(_env_file(tmp_path, text))) == expected


def test_proxy_parser_auth_blank_password_and_reserved_unicode(tmp_path: Path) -> None:
    text = """\
SERVERFS_PROXY_HOST=proxy.internal
SERVERFS_PROXY_PORT=7890
SERVERFS_PROXY_USERNAME="a b=#$@:/%雪"
SERVERFS_PROXY_PASSWORD='p a=#$@:/%雪'
"""
    values = parse_proxy_env_file(_env_file(tmp_path, text))
    expected = "http://%61%20%62%3d%23%24%40%3a%2f%25%e9%9b%aa:%70%20%61%3d%23%24%40%3a%2f%25%e9%9b%aa@proxy.internal:7890"
    assert proxy_url(values) == expected
    values["SERVERFS_PROXY_USERNAME"] = "user"
    values["SERVERFS_PROXY_PASSWORD"] = ""
    assert proxy_url(values) == "http://%75%73%65%72:@proxy.internal:7890"


def test_proxy_parser_preserves_unquoted_hash_equals_and_double_quote_escapes(
    tmp_path: Path,
) -> None:
    path = _env_file(
        tmp_path,
        'SERVERFS_PROXY_USERNAME=a b=#@:/%雪\nSERVERFS_PROXY_PASSWORD="x\\"y\\\\z" # note\n',
    )
    parsed = parse_proxy_env_file(path)
    assert parsed["SERVERFS_PROXY_USERNAME"] == "a b=#@:/%雪"
    assert parsed["SERVERFS_PROXY_PASSWORD"] == 'x"y\\z'


@pytest.mark.parametrize(
    "text",
    [
        "SERVERFS_PROXY_PORT=80\n",
        "SERVERFS_PROXY_USERNAME=user\n",
        "SERVERFS_PROXY_PASSWORD=secret-sentinel\n",
        "SERVERFS_PROXY_HOST=proxy.internal\n",
        "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=0\n",
        "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=65536\n",
        "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=nope\n",
        "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=80\nSERVERFS_PROXY_PASSWORD=secret-sentinel\n",
    ],
)
def test_invalid_proxy_combinations_are_redacted(tmp_path: Path, text: str) -> None:
    values = parse_proxy_env_file(_env_file(tmp_path, text))
    with pytest.raises(NativeTunnelError) as excinfo:
        proxy_url(values)
    assert "secret-sentinel" not in str(excinfo.value)


def test_duplicate_target_keys_fail_redacted(tmp_path: Path) -> None:
    path = _env_file(
        tmp_path,
        "SERVERFS_PROXY_PASSWORD=secret-sentinel\nSERVERFS_PROXY_PASSWORD=other-secret\n",
    )
    with pytest.raises(NativeTunnelError) as excinfo:
        parse_proxy_env_file(path)
    assert "duplicate SERVERFS_PROXY_PASSWORD" in str(excinfo.value)
    assert "secret-sentinel" not in str(excinfo.value)
    assert "other-secret" not in str(excinfo.value)


def test_bad_quoting_errors_do_not_include_values(tmp_path: Path) -> None:
    with pytest.raises(NativeTunnelError) as excinfo:
        parse_proxy_env_file(_env_file(tmp_path, 'SERVERFS_PROXY_PASSWORD="secret-sentinel\n'))
    assert "secret-sentinel" not in str(excinfo.value)


@pytest.mark.parametrize(
    "argv",
    [
        [r"C:\Program Files\Python\python.exe", "-m", "serverfs_mcp.supervisor"],
        [r"C:\Users\A B\Python 3.12\python.exe", "a b", r"x\y\z"],
        [r"C:\Users\A B\Python,Tools\python.exe", "-c", "print(1,2,3)", r"D:\A B\x"],
        [
            (
                r"C:\Program Files\bin,channel=main,http-proxy=x,url=x,"
                r"unix-socket=x,client-cert=x,client-key=x.exe"
            ),
            "--config",
            (
                r"D:\Work Dir\cfg,channel=tools,http-proxy=p,url=u,"
                r"unix-socket=s,client-cert=c,client-key=k.toml"
            ),
        ],
        [
            r"C:\Python 3.12\python.exe",
            "-c",
            "print(',channel=main,http-proxy=x,url=x,unix-socket=x,client-cert=x,client-key=x')",
        ],
        [r"C:\a\quoted\app.exe", 'argument with "quotes"', "trailing\\"],
        [r"\\server\share\Program Files\python.exe", "--config", r"D:\Work Dir\serverfs.toml"],
    ],
)
def test_tunnel_command_encoder_round_trips_upstream_parser(argv: list[str]) -> None:
    # Upstream pkg/runtimeconfig/config.go sends unqualified values directly
    # to parseCommandArgv; qualified entries scan raw commas before parsing.
    encoded = encode_tunnel_command_argv(argv)
    assert _parse_upstream_stdio_argv(encoded) == argv


def _parse_upstream_stdio_argv(raw: str) -> list[str]:
    """Small exact port of tunnel-client/pkg/runtimeconfig.parseCommandArgv."""
    args: list[str] = []
    builder: list[str] = []
    in_single = in_double = escaped = False
    for char in raw.strip():
        if escaped:
            builder.append(char)
            escaped = False
        elif in_single:
            if char == "'":
                in_single = False
            else:
                builder.append(char)
        elif in_double:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            else:
                builder.append(char)
        elif char == "\\":
            escaped = True
        elif char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char in " \t\n\r":
            if builder:
                args.append("".join(builder))
                builder.clear()
        else:
            builder.append(char)
    assert not (escaped or in_single or in_double)
    if builder:
        args.append("".join(builder))
    return args


def test_native_proxy_url_matches_linux_percent_encoding_contract() -> None:
    values = {
        "SERVERFS_PROXY_HOST": "proxy.internal",
        "SERVERFS_PROXY_PORT": "7890",
        "SERVERFS_PROXY_USERNAME": "a@:/% b",
        "SERVERFS_PROXY_PASSWORD": "p@:/% x",
    }
    assert proxy_url(values) == (
        "http://%61%40%3a%2f%25%20%62:%70%40%3a%2f%25%20%78@proxy.internal:7890"
    )
    assert proxy_url({"SERVERFS_PROXY_HOST": "proxy.internal", "SERVERFS_PROXY_PORT": "80"}) == (
        "http://proxy.internal:80"
    )


def test_tunnel_gets_proxy_only_in_internal_env_not_argv(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(f'[[workdirs]]\nalias="repo"\npath="{root.as_posix()}"\n', encoding="utf-8")
    key = tmp_path / "api-key.txt"
    key.write_text("key-sentinel", encoding="utf-8")
    client = tmp_path / "tunnel-client.exe"
    client.touch()
    env_file = _env_file(
        tmp_path,
        "SERVERFS_PROXY_HOST=proxy.internal\nSERVERFS_PROXY_PORT=8080\n"
        "SERVERFS_PROXY_USERNAME=user@name\nSERVERFS_PROXY_PASSWORD=pw#secret\n",
    )
    captured: dict[str, object] = {}

    def fake_run(argv, *, env, check):
        captured.update(argv=argv, env=env, check=check)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(native_tunnel.sys, "platform", "win32")
    monkeypatch.setattr(native_tunnel.subprocess, "run", fake_run)
    monkeypatch.setenv("CONTROL_PLANE_HTTP_PROXY", "old-control-proxy")
    monkeypatch.setenv("TUNNEL_CLIENT_HTTP_PROXY", "old-global-proxy")
    monkeypatch.setenv("http_proxy", "old-lower-http-proxy")
    monkeypatch.setenv("HTTPS_PROXY", "old-generic-proxy")
    monkeypatch.setenv("https_proxy", "old-lower-https-proxy")
    monkeypatch.setenv("ALL_PROXY", "old-generic-all-proxy")
    monkeypatch.setenv("all_proxy", "old-lower-all-proxy")
    monkeypatch.setenv("NO_PROXY", "old-no-proxy")
    monkeypatch.setenv("no_proxy", "old-lower-no-proxy")
    monkeypatch.setenv("SERVERFS_PROXY_HOST", "inherited-host")
    monkeypatch.setenv("SERVERFS_PROXY_PORT", "1234")
    monkeypatch.setenv("SERVERFS_PROXY_USERNAME", "inherited-username")
    monkeypatch.setenv("SERVERFS_PROXY_PASSWORD", "inherited-raw-password")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "inherited-api-key")
    monkeypatch.setenv("CONTROL_PLANE_TUNNEL_ID", "inherited-tunnel-id")
    monkeypatch.setenv("MCP_COMMAND", "inherited-mcp-command")
    monkeypatch.setenv("MCP_SERVER_URL", "inherited-mcp-url")
    monkeypatch.setenv("TUNNEL_CLIENT_PROFILE", "inherited-profile")
    monkeypatch.setenv("openai_api_key", "inherited-lower-openai-key")

    assert (
        run_native_tunnel(
            config_path=config,
            env_file=env_file,
            tunnel_client=client,
            tunnel_id="tunnel_" + "a" * 32,
            api_key_file=key,
        )
        == 0
    )
    argv = captured["argv"]
    env = captured["env"]
    assert isinstance(argv, list) and isinstance(env, dict)
    assert "--control-plane.http-proxy" not in argv
    command_index = argv.index("--mcp.command")
    assert argv[command_index + 1].startswith('"')
    assert not argv[command_index + 1].startswith("command=")
    assert "pw#secret" not in " ".join(argv)
    assert "user@name" not in " ".join(argv)
    assert (
        env["CONTROL_PLANE_HTTP_PROXY"]
        == "http://%75%73%65%72%40%6e%61%6d%65:%70%77%23%73%65%63%72%65%74@proxy.internal:8080"
    )
    for name in (
        "CONTROL_PLANE_HTTP_PROXY",
        "TUNNEL_CLIENT_HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "SERVERFS_PROXY_HOST",
        "SERVERFS_PROXY_PORT",
        "SERVERFS_PROXY_USERNAME",
        "SERVERFS_PROXY_PASSWORD",
        "CONTROL_PLANE_API_KEY",
        "CONTROL_PLANE_TUNNEL_ID",
        "MCP_COMMAND",
        "MCP_SERVER_URL",
        "TUNNEL_CLIENT_PROFILE",
        "openai_api_key",
    ):
        assert name not in env or name == "CONTROL_PLANE_HTTP_PROXY"
    assert f"file:{key.resolve()}" in argv
    assert "key-sentinel" not in " ".join(argv)


def test_api_key_file_inside_workdir_is_refused(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(f'[[workdirs]]\nalias="repo"\npath="{root.as_posix()}"\n')
    key = root / "secret.txt"
    key.write_text("secret")
    client = tmp_path / "tunnel-client.exe"
    client.touch()
    monkeypatch.setattr(native_tunnel.sys, "platform", "win32")
    with pytest.raises(NativeTunnelError, match="outside every configured workdir"):
        run_native_tunnel(
            config_path=config,
            env_file=None,
            tunnel_client=client,
            tunnel_id="tunnel_" + "a" * 32,
            api_key_file=key,
        )


def test_supervisor_removes_tunnel_and_proxy_environment() -> None:
    source = {
        "SystemRoot": "C:\\Windows",
        "PATH": "C:\\Python",
        "TEMP": "C:\\Temp",
        "USERPROFILE": "C:\\Users\\test",
        "APPDATA": "C:\\Users\\test\\AppData",
        "LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local",
        "PYTHONPATH": "C:\\project",
        "CONTROL_PLANE_API_KEY": "key-sentinel",
        "CONTROL_PLANE_HTTP_PROXY": "proxy-sentinel",
        "TUNNEL_CLIENT_HTTP_PROXY": "proxy-sentinel",
        "OPENAI_API_KEY": "openai-sentinel",
        "MCP_COMMAND": "binding-sentinel",
        "SERVERFS_PROXY_PASSWORD": "password-sentinel",
        "http_proxy": "generic-proxy-sentinel",
        "HTTPS_PROXY": "generic-proxy-sentinel",
        "all_proxy": "generic-proxy-sentinel",
        "NO_PROXY": "generic-proxy-sentinel",
        "no_proxy": "generic-proxy-sentinel",
    }
    clean = sanitized_environment(source)
    assert {k: clean[k] for k in source if k in clean} == {
        key: source[key]
        for key in (
            "SystemRoot",
            "PATH",
            "TEMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PYTHONPATH",
        )
    }


def test_forward_stdio_child_environment_and_byte_transparency(monkeypatch) -> None:
    import io

    source_env = {
        **os.environ,
        "CONTROL_PLANE_API_KEY": "key-sentinel",
        "CONTROL_PLANE_HTTP_PROXY": "proxy-sentinel",
        "SERVERFS_PROXY_PASSWORD": "password-sentinel",
        "HTTPS_PROXY": "https-sentinel",
        "NO_PROXY": "no-proxy-sentinel",
        "no_proxy": "no-proxy-sentinel",
    }
    child_env = sanitized_environment(source_env)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"frame\x00bytes")))
    captured = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(captured))
    probe = (
        "import os,sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data + b'|' + b','.join(k.encode() for k in os.environ "
        "if k in {'CONTROL_PLANE_API_KEY','CONTROL_PLANE_HTTP_PROXY','SERVERFS_PROXY_PASSWORD',"
        "'HTTPS_PROXY','NO_PROXY','no_proxy'}))"
    )
    code = forward_stdio([sys.executable, "-c", probe], child_env)
    assert code == 0
    assert captured.getvalue() == b"frame\x00bytes|"


def test_supervisor_copy_forwards_small_pipe_payload_before_eof() -> None:
    payload = b"Content-Length: 2\r\n\r\n{}"
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=64 * 1024)

    class Destination:
        def __init__(self) -> None:
            self.data = bytearray()
            self.written = threading.Event()

        def write(self, data: bytes) -> int:
            self.data.extend(data)
            self.written.set()
            return len(data)

        def flush(self) -> None:
            pass

    destination = Destination()
    worker = threading.Thread(target=_copy, args=(source, destination), daemon=True)
    try:
        assert os.write(write_fd, payload) == len(payload)
        worker.start()
        assert destination.written.wait(timeout=1), "pipe payload was buffered until EOF"
        assert bytes(destination.data) == payload
    finally:
        os.close(write_fd)
        if worker.ident is not None:
            worker.join(timeout=1)
        source.close()
    assert not worker.is_alive(), "copy thread did not exit after pipe EOF"


def test_supervisor_copy_falls_back_to_read_for_test_doubles() -> None:
    class ReadOnlySource:
        def __init__(self) -> None:
            self.chunks = iter((b"fallback bytes", b""))

        def read(self, size: int) -> bytes:
            assert size == 64 * 1024
            return next(self.chunks)

    class Destination:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, data: bytes) -> int:
            self.data.extend(data)
            return len(data)

        def flush(self) -> None:
            pass

    destination = Destination()
    _copy(ReadOnlySource(), destination)  # type: ignore[arg-type]
    assert destination.data == b"fallback bytes"


def _launcher_capture(tmp_path, monkeypatch, **kwargs):
    root = tmp_path / "work"
    root.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_text(f'[[workdirs]]\nalias="repo"\npath="{root.as_posix()}"\n', encoding="utf-8")
    key = tmp_path / "api-key.txt"
    key.write_text("key-sentinel", encoding="utf-8")
    client = tmp_path / "tunnel-client.exe"
    client.touch()
    captured: dict[str, object] = {}

    def fake_run(argv, *, env, check):
        captured.update(argv=argv, env=env, check=check)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(native_tunnel.sys, "platform", "win32")
    monkeypatch.setattr(native_tunnel.subprocess, "run", fake_run)
    monkeypatch.setenv("CONTROL_PLANE_BASE_URL", "https://inherited.example")
    monkeypatch.setenv("HEALTH_LISTEN_ADDR", "127.0.0.1:9999")
    code = run_native_tunnel(
        config_path=config,
        env_file=None,
        tunnel_client=client,
        tunnel_id="tunnel_" + "b" * 32,
        api_key_file=key,
        **kwargs,
    )
    assert code == 0
    return captured


def test_launcher_forces_ephemeral_health_and_sanitors_base_url(tmp_path, monkeypatch) -> None:
    captured = _launcher_capture(tmp_path, monkeypatch)
    env = captured["env"]
    assert env["HEALTH_LISTEN_ADDR"] == "127.0.0.1:0"
    assert "CONTROL_PLANE_BASE_URL" not in env  # inherited value stripped, none set
    argv = captured["argv"]
    assert "--control-plane.base-url" not in argv
    assert "--health.listen-addr" not in argv


def test_launcher_applies_validated_base_url_and_health_overrides(tmp_path, monkeypatch) -> None:
    captured = _launcher_capture(
        tmp_path,
        monkeypatch,
        base_url="https://127.0.0.1:8443",
        health_listen_addr="127.0.0.1:18080",
    )
    env = captured["env"]
    assert env["CONTROL_PLANE_BASE_URL"] == "https://127.0.0.1:8443"
    assert env["HEALTH_LISTEN_ADDR"] == "127.0.0.1:18080"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("http://insecure.example", ""),
        ("https://user:sup3rsecret@host", "sup3rsecret"),
        ("https://host/path", ""),
        ("https://host?token=abc", "abc"),
        ("ftp://host", ""),
        ("https://", ""),
        ("https://host:0", ""),
        ("https://host:notaport", ""),
    ],
)
def test_base_url_validation_is_fail_closed_and_redacted(raw: str, secret: str) -> None:
    with pytest.raises(NativeTunnelError) as excinfo:
        native_tunnel._validated_base_url(raw)
    if secret:
        assert secret not in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        "8080",
        "host:notaport",
        "host:70000",
        "0.0.0.0:8080",
        ":8080",
        "192.168.1.10:8080",
        "localhost:8080",
    ],
)
def test_health_addr_validation_is_fail_closed(raw: str) -> None:
    with pytest.raises(NativeTunnelError):
        native_tunnel._validated_health_addr(raw)


@pytest.mark.parametrize("raw", ["127.0.0.1:0", "127.0.0.1:18080", "127.0.0.1:65535"])
def test_health_addr_accepts_loopback_ports(raw: str) -> None:
    assert native_tunnel._validated_health_addr(raw) == raw
