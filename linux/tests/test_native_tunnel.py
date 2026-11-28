"""Linux .env/proxy parsing and upstream argument encoding contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.native_tunnel import (
    NativeTunnelError,
    encode_tunnel_command_argv,
    parse_proxy_env_file,
    proxy_url,
)


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
