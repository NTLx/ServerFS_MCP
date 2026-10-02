"""Narrow native launcher for the official OpenAI tunnel-client."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROXY_KEYS = (
    "SERVERFS_PROXY_HOST",
    "SERVERFS_PROXY_PORT",
    "SERVERFS_PROXY_USERNAME",
    "SERVERFS_PROXY_PASSWORD",
)
_TUNNEL_ID = re.compile(r"^tunnel_[a-z0-9]{32}$")


class NativeTunnelError(ValueError):
    """Redacted, operator-safe tunnel configuration error."""


def parse_proxy_env_file(path: Path | None) -> dict[str, str]:
    """Read only the four shared proxy values, without expansion or evaluation."""
    if path is None:
        return {}
    if not path.exists():
        raise NativeTunnelError("proxy env file does not exist")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise NativeTunnelError("cannot read proxy env file") from exc

    for line_number, line in enumerate(lines, 1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        key, sep, raw = candidate.partition("=")
        key = key.strip()
        if key not in PROXY_KEYS:
            continue
        if key in values:
            raise NativeTunnelError(f"duplicate {key} at line {line_number}")
        if not sep:
            raise NativeTunnelError(f"invalid {key} assignment at line {line_number}")
        values[key] = _parse_env_value(raw.strip(), key, line_number)
    return values


def _parse_env_value(raw: str, key: str, line_number: int) -> str:
    if not raw or raw[0] not in "\"'":
        return raw.strip()
    quote = raw[0]
    out: list[str] = []
    escaped = False
    index = 1
    while index < len(raw):
        char = raw[index]
        if quote == '"' and escaped:
            if char not in {'"', "\\"}:
                out.append("\\")
            out.append(char)
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif char == quote:
            tail = raw[index + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise NativeTunnelError(f"invalid quoted {key} at line {line_number}")
            return "".join(out)
        else:
            out.append(char)
        index += 1
    raise NativeTunnelError(f"unterminated quoted {key} at line {line_number}")


def proxy_url(values: dict[str, str]) -> str | None:
    """Validate shared fields and derive the Linux-contract HTTP proxy URL."""
    host = values.get("SERVERFS_PROXY_HOST", "")
    port = values.get("SERVERFS_PROXY_PORT", "")
    username = values.get("SERVERFS_PROXY_USERNAME", "")
    password = values.get("SERVERFS_PROXY_PASSWORD", "")
    if not host:
        if port:
            raise NativeTunnelError("proxy PORT requires HOST")
        if username:
            raise NativeTunnelError("proxy USERNAME requires HOST")
        if password:
            raise NativeTunnelError("proxy PASSWORD requires HOST")
        return None
    if not port:
        raise NativeTunnelError("proxy HOST requires PORT")
    if (
        any(not (char.isascii() and (char.isalnum() or char in ".-")) for char in host)
        or host.startswith(".")
        or host.endswith((".", "-"))
        or ".." in host
        or "-." in host
        or ".-" in host
        or host.startswith("-")
    ):
        raise NativeTunnelError("proxy HOST must be a DNS name or IPv4 address")
    if not port.isascii() or not port.isdecimal():
        raise NativeTunnelError("proxy PORT must be an integer from 1 to 65535")
    normalized_port = int(port)
    if not 1 <= normalized_port <= 65535:
        raise NativeTunnelError("proxy PORT must be an integer from 1 to 65535")
    if password and not username:
        raise NativeTunnelError("proxy PASSWORD requires USERNAME")
    authority = f"{host}:{normalized_port}"
    if username:
        authority = f"{_percent_encode(username)}:{_percent_encode(password)}@{authority}"
    return f"http://{authority}"


def _percent_encode(value: str) -> str:
    return "".join(f"%{byte:02x}" for byte in value.encode("utf-8"))


def encode_tunnel_command_argv(argv: list[str]) -> str:
    """Encode argv for tunnel-client's parseCommandArgv, even on Windows.

    That parser treats each backslash as an escape except inside single
    quotes, where embedded single quotes cannot be represented. Double
    quotes with doubled backslashes and escaped double quotes round-trip.
    """
    if not argv or any(not arg for arg in argv):
        raise NativeTunnelError("tunnel command arguments must be non-empty")
    return " ".join('"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' for arg in argv)


def _api_key_is_outside_workdirs(api_key_path: Path, config_path: Path) -> bool:
    from .native_config import NativeConfigError, load_native_config

    try:
        workdirs, _ = load_native_config(config_path)
        key_real = api_key_path.resolve(strict=True)
        roots = [wd.root.resolve(strict=True) for wd in workdirs]
    except (OSError, NativeConfigError, RuntimeError) as exc:
        raise NativeTunnelError("cannot validate API-key location against native workdirs") from exc
    if not key_real.is_file():
        raise NativeTunnelError("API-key path must name a file")
    for root in roots:
        try:
            key_real.relative_to(root)
        except ValueError:
            continue
        raise NativeTunnelError("API-key file must be outside every configured workdir")
    return True


def run_native_tunnel(
    *,
    config_path: Path,
    env_file: Path | None,
    tunnel_client: Path,
    tunnel_id: str,
    api_key_file: Path,
) -> int:
    """Run tunnel-client, whose stdio child is the minimal sanitizer supervisor."""
    if sys.platform != "win32":
        raise NativeTunnelError("native tunnel is supported only on Windows")
    if not _TUNNEL_ID.fullmatch(tunnel_id):
        raise NativeTunnelError("invalid tunnel ID")
    if not config_path.is_file():
        raise NativeTunnelError("native config file does not exist")
    if not tunnel_client.is_file():
        raise NativeTunnelError("tunnel-client executable does not exist")
    key_path = api_key_file.resolve(strict=True)
    _api_key_is_outside_workdirs(key_path, config_path)
    env_path = env_file
    if env_path is None:
        sibling = config_path.resolve().parent / ".env"
        env_path = sibling if sibling.exists() else None
    proxy = proxy_url(parse_proxy_env_file(env_path))

    child = [
        sys.executable,
        "-m",
        "serverfs_mcp.supervisor",
        "--config",
        str(config_path.resolve()),
    ]
    command_entry = encode_tunnel_command_argv(child)
    tunnel_argv = [
        str(tunnel_client),
        "run",
        "--control-plane.tunnel-id",
        tunnel_id,
        "--control-plane.api-key",
        f"file:{key_path}",
        "--mcp.command",
        command_entry,
    ]
    tunnel_env = os.environ.copy()
    tunnel_env_prefixes = (
        "CONTROL_PLANE_",
        "TUNNEL_CLIENT_",
        "MCP_",
        "OPENAI_",
        "SERVERFS_PROXY_",
    )
    proxy_env_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    for name in list(tunnel_env):
        if name.upper().startswith(tunnel_env_prefixes) or name.lower() in proxy_env_names:
            tunnel_env.pop(name, None)
    if proxy:
        tunnel_env["CONTROL_PLANE_HTTP_PROXY"] = proxy
    return subprocess.run(tunnel_argv, env=tunnel_env, check=False).returncode
