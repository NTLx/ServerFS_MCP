"""Tunnel-only HTTP proxy configuration and launcher contract."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "deployment" / "tunnel" / "tunnel-launcher.sh"


def _run_launcher(tmp_path: Path, **proxy_env: str) -> subprocess.CompletedProcess[str]:
    mock_client = tmp_path / "tunnel-client"
    mock_client.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "${CONTROL_PLANE_HTTP_PROXY-<unset>}" > "$PROXY_RESULT"\n'
        'printf \'%s\\n\' "$*" > "$ARGS_RESULT"\n',
        encoding="utf-8",
    )
    mock_client.chmod(0o755)

    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        _LAUNCHER.read_text(encoding="utf-8").replace(
            "exec /usr/bin/tunnel-client run",
            'exec "$MOCK_TUNNEL_CLIENT" run',
        ),
        encoding="utf-8",
    )

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SERVERFS_PROXY_") and key != "CONTROL_PLANE_HTTP_PROXY"
    }
    env.update(
        {
            "PROXY_RESULT": str(tmp_path / "proxy-result"),
            "ARGS_RESULT": str(tmp_path / "args-result"),
            "MOCK_TUNNEL_CLIENT": str(mock_client),
            **proxy_env,
        }
    )
    return subprocess.run(
        ["/bin/sh", str(launcher)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_proxy_disabled_unsets_derived_value_and_runs_tunnel(tmp_path: Path) -> None:
    result = _run_launcher(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "proxy-result").read_text(encoding="utf-8").strip() == "<unset>"
    assert (tmp_path / "args-result").read_text(encoding="utf-8").strip() == "run"


def test_proxy_without_authentication_has_no_userinfo(tmp_path: Path) -> None:
    result = _run_launcher(
        tmp_path,
        SERVERFS_PROXY_HOST="proxy.internal",
        SERVERFS_PROXY_PORT="0080",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "proxy-result").read_text(encoding="utf-8").strip() == (
        "http://proxy.internal:80"
    )


def test_proxy_auth_percent_encodes_reserved_characters_and_spaces(tmp_path: Path) -> None:
    result = _run_launcher(
        tmp_path,
        SERVERFS_PROXY_HOST="proxy.internal",
        SERVERFS_PROXY_PORT="7890",
        SERVERFS_PROXY_USERNAME="a@:/% b",
        SERVERFS_PROXY_PASSWORD="p@:/% x",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "proxy-result").read_text(encoding="utf-8").strip() == (
        "http://%61%40%3a%2f%25%20%62:%70%40%3a%2f%25%20%78@proxy.internal:7890"
    )


def test_proxy_username_enables_authentication_with_empty_password(tmp_path: Path) -> None:
    result = _run_launcher(
        tmp_path,
        SERVERFS_PROXY_HOST="proxy.internal",
        SERVERFS_PROXY_PORT="7890",
        SERVERFS_PROXY_USERNAME="user",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "proxy-result").read_text(encoding="utf-8").strip() == (
        "http://%75%73%65%72:@proxy.internal:7890"
    )


@pytest.mark.parametrize(
    ("proxy_env", "message"),
    [
        ({"SERVERFS_PROXY_PORT": "80"}, "PORT requires HOST"),
        ({"SERVERFS_PROXY_USERNAME": "user"}, "USERNAME requires HOST"),
        ({"SERVERFS_PROXY_PASSWORD": "secret"}, "PASSWORD requires HOST"),
        ({"SERVERFS_PROXY_HOST": "proxy.internal"}, "HOST requires PORT"),
        (
            {"SERVERFS_PROXY_HOST": "proxy.internal", "SERVERFS_PROXY_PORT": "0"},
            "PORT must be an integer",
        ),
        (
            {"SERVERFS_PROXY_HOST": "proxy.internal", "SERVERFS_PROXY_PORT": "65536"},
            "PORT must be an integer",
        ),
        (
            {"SERVERFS_PROXY_HOST": "proxy.internal", "SERVERFS_PROXY_PORT": "abc"},
            "PORT must be an integer",
        ),
        (
            {
                "SERVERFS_PROXY_HOST": "proxy.internal",
                "SERVERFS_PROXY_PORT": "80",
                "SERVERFS_PROXY_PASSWORD": "secret-value",
            },
            "PASSWORD requires USERNAME",
        ),
    ],
)
def test_invalid_proxy_configuration_fails_without_echoing_values(
    tmp_path: Path, proxy_env: dict[str, str], message: str
) -> None:
    result = _run_launcher(tmp_path, **proxy_env)

    assert result.returncode == 2
    assert message in result.stderr
    assert "secret-value" not in result.stderr


def test_compose_scopes_proxy_environment_to_openai_tunnel() -> None:
    compose = (_REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    server = compose.split("  serverfs-mcp:\n", 1)[1].split("  serverfs-file-ingress:\n", 1)[0]
    ingress = compose.split("  serverfs-file-ingress:\n", 1)[1].split("  openai-tunnel:\n", 1)[0]
    tunnel = compose.split("  openai-tunnel:\n", 1)[1].split("\n\nnetworks:\n", 1)[0]

    for variable in (
        "SERVERFS_PROXY_HOST",
        "SERVERFS_PROXY_PORT",
        "SERVERFS_PROXY_USERNAME",
        "SERVERFS_PROXY_PASSWORD",
    ):
        assert variable not in server
        assert variable not in ingress
        assert variable in tunnel
    assert "CONTROL_PLANE_HTTP_PROXY" not in compose
    assert re.search(r"https?://[^/\s]+@[^/\s]+", compose) is None
    assert "tunnel-launcher.sh:ro" in tunnel
    assert 'entrypoint: ["/bin/sh", "/opt/serverfs/tunnel-launcher.sh"]' in tunnel


def test_env_example_documents_only_project_proxy_inputs() -> None:
    env_example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "CONTROL_PLANE_HTTP_PROXY=" not in env_example
    for variable in (
        "SERVERFS_PROXY_HOST=",
        "SERVERFS_PROXY_PORT=",
        "SERVERFS_PROXY_USERNAME=",
        "SERVERFS_PROXY_PASSWORD=",
    ):
        assert env_example.count(variable) == 1
