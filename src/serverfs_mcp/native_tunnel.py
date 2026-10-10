"""Narrow native launcher for the official OpenAI tunnel-client."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .shared_proxy import PROXY_KEYS, parse_shared_proxy

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
    """Validate shared fields and derive the canonical HTTP proxy URL."""
    parsed = parse_shared_proxy(values, error_type=NativeTunnelError)
    return None if parsed is None else parsed.url


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


def _materialize_key_file(api_key: str) -> Path:
    """Write a key VALUE to a 0600 private file and return its path.

    The operator may keep the control-plane key directly in ``.env``
    (``CONTROL_PLANE_API_KEY``). The ``file:`` credential boundary of the
    tunnel argv is preserved anyway: the launcher materializes the value
    into a private file outside every workdir instead of putting the
    secret into argv (visible in ``ps``) or the child environment. The
    runtime location is the OS-provided per-user runtime directory on
    macOS; Windows deployments keep the explicit key-file flow.
    """
    if sys.platform == "darwin":
        from .darwin_libc import darwin_runtime_dir

        directory = darwin_runtime_dir() / "control-plane"
    else:
        raise NativeTunnelError("on this platform, pass the control-plane key as an --api-key-file")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / "api-key"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, api_key.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return key_path


def run_native_tunnel(
    *,
    config_path: Path,
    env_file: Path | None,
    tunnel_client: Path | None,
    tunnel_id: str,
    api_key_file: Path | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    health_listen_addr: str | None = None,
) -> int:
    """Run tunnel-client, whose stdio child is the minimal sanitizer supervisor.

    The control-plane key comes from exactly one source: ``--api-key-file``
    (the frozen Windows flow) or, on macOS, the ``CONTROL_PLANE_API_KEY``
    value (materialized to a 0600 private file so the argv keeps the
    ``file:`` credential boundary).
    """
    if sys.platform == "linux":
        raise NativeTunnelError(
            "the Linux deployment runs the Docker compose topology; the native "
            "tunnel launcher is supported on Windows and macOS 27"
        )
    if sys.platform == "darwin":
        from .darwin_platform import ensure_supported_darwin

        ensure_supported_darwin()
    if base_url is not None:
        base_url = _validated_base_url(base_url)
    if health_listen_addr is not None:
        health_listen_addr = _validated_health_addr(health_listen_addr)
    if tunnel_client is None:
        from .tunnel_bootstrap import default_tunnel_client_path

        tunnel_client = default_tunnel_client_path()
        if tunnel_client is None:
            raise NativeTunnelError(
                "no bootstrapped tunnel-client found; run 'serverfs bootstrap tunnel-client' "
                "or pass --tunnel-client"
            )
    if not _TUNNEL_ID.fullmatch(tunnel_id):
        raise NativeTunnelError("invalid tunnel ID")
    if not config_path.is_file():
        raise NativeTunnelError("native config file does not exist")
    if not tunnel_client.is_file():
        raise NativeTunnelError("tunnel-client executable does not exist")
    if api_key is not None and api_key_file is not None:
        raise NativeTunnelError(
            "provide the control-plane key exactly one way: CONTROL_PLANE_API_KEY or --api-key-file"
        )
    if api_key is not None:
        if not api_key.strip():
            raise NativeTunnelError("CONTROL_PLANE_API_KEY is empty")
        key_path = _materialize_key_file(api_key)
    elif api_key_file is not None:
        key_path = api_key_file.resolve(strict=True)
    else:
        raise NativeTunnelError(
            "an API key is required: set CONTROL_PLANE_API_KEY in the environment "
            "or pass --api-key-file"
        )
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
    if base_url:
        tunnel_env["CONTROL_PLANE_BASE_URL"] = base_url
    # tunnel-client's health server defaults to 127.0.0.1:8080, which collides
    # with ordinary local services (measured in Phase F acceptance). The
    # launcher owns this connectivity infrastructure, so it requests an
    # ephemeral loopback port unless the operator overrides it.
    tunnel_env["HEALTH_LISTEN_ADDR"] = health_listen_addr or "127.0.0.1:0"
    return subprocess.run(tunnel_argv, env=tunnel_env, check=False).returncode


def _validated_health_addr(raw: str) -> str:
    # Loopback-only by contract: the native profile freezes "no LAN
    # exposure" — the health server may bind an ephemeral or fixed port on
    # 127.0.0.1 and nothing else. IPv6 loopback is deliberately not parsed
    # until there is an explicit need.
    candidate = raw.strip()
    host, sep, port = candidate.rpartition(":")
    if (
        host != "127.0.0.1"
        or not sep
        or not port.isascii()
        or not port.isdecimal()
        or not 0 <= int(port) <= 65535
    ):
        raise NativeTunnelError("health listen address must be 127.0.0.1:<port 0..65535>")
    return candidate


def _validated_base_url(raw: str) -> str:
    """Deployment-facing override of the control-plane endpoint (acceptance
    harnesses, future regional endpoints). HTTPS-only, no userinfo, no path:
    this value selects the trust anchor of the control-plane connection, so
    anything looser is refused rather than normalized."""
    from urllib.parse import urlsplit

    split = urlsplit(raw.strip())
    if (
        split.scheme != "https"
        or not split.hostname
        or split.username
        or split.password
        or (split.path and split.path != "/")
        or split.query
        or split.fragment
    ):
        raise NativeTunnelError("base URL must be an https endpoint without credentials or path")
    try:
        port = split.port
    except ValueError as exc:
        raise NativeTunnelError("base URL port must be an integer from 1 to 65535") from exc
    if port is not None and not 1 <= port <= 65535:
        raise NativeTunnelError("base URL port must be an integer from 1 to 65535")
    return raw.strip().rstrip("/")
