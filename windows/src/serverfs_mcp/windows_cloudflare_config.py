"""Validated opt-in Windows Cloudflare provider configuration (no OS mutation)."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .cloudflare_bootstrap import (
    VERSION,
    WINDOWS_BINARY_SHA256,
    _sha256,
    _verify_windows_binary,
    serverfs_data_dir,
)
from .windows_cloudflare_security import (
    _no_reparse_components,
    validate_private_file,
)

_HTTP_ORIGIN_ENV_KEYS = frozenset(
    {
        "SERVERFS_HTTP_API_KEY",
        "SERVERFS_HTTP_ALLOWED_HOSTS",
        "SERVERFS_OAUTH_ENABLED",
        "SERVERFS_OAUTH_RESOURCE_URL",
        "SERVERFS_OAUTH_ISSUER_URL",
        "SERVERFS_OAUTH_REQUIRED_SCOPES",
        "SERVERFS_OAUTH_INTROSPECTION_URL",
        "SERVERFS_OAUTH_INTROSPECTION_SECRET",
        "SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS",
    }
)
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class WindowsCloudflareConfigurationError(Exception):
    """Safe operator-facing error; never includes secret values."""


@dataclass(frozen=True)
class CloudflareWindowsSpec:
    config_path: Path
    cloudflared: Path
    token_file: Path
    env_file: Path
    port: int
    hostname: str
    origin_environment: dict[str, str] = field(repr=False)


def _port_available(port: int) -> bool:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _validate_origin_environment(values: dict[str, str], hostname: str) -> dict[str, str]:
    if set(values) - _HTTP_ORIGIN_ENV_KEYS:
        raise WindowsCloudflareConfigurationError("HTTP origin env file has unsupported keys")
    if values.get("SERVERFS_HTTP_ALLOWED_HOSTS") != hostname:
        raise WindowsCloudflareConfigurationError(
            "HTTP allowed hosts must exactly match public hostname"
        )
    from .config import settings_from_env

    try:
        settings = settings_from_env(values)
    except ValueError as exc:
        raise WindowsCloudflareConfigurationError(
            "HTTP origin auth configuration is invalid"
        ) from exc
    key = bool(settings.http_api_key)
    oauth = settings.oauth_enabled
    if key == oauth:
        raise WindowsCloudflareConfigurationError("configure exactly one API Key or OAuth")
    if oauth and not values.get("SERVERFS_OAUTH_INTROSPECTION_SECRET", "").strip():
        raise WindowsCloudflareConfigurationError("OAuth introspection secret is required")
    return dict(values)


def validate_install(
    config_path: Path,
    cloudflared: Path,
    token_file: Path,
    env_file: Path,
    port: int,
    hostname: str,
    *,
    user_sid: str,
) -> CloudflareWindowsSpec:
    """Fail closed before any scheduled-task or manifest mutation."""
    if (
        not 1 <= port <= 65535
        or not hostname
        or len(hostname) > 253
        or not all(_LABEL.fullmatch(label) for label in hostname.split("."))
    ):
        raise WindowsCloudflareConfigurationError("invalid HTTP origin port or hostname")
    if not _port_available(port):
        raise WindowsCloudflareConfigurationError("HTTP origin loopback port is unavailable")
    config = Path(config_path).expanduser()
    if not config.is_absolute():
        raise WindowsCloudflareConfigurationError("native config must have an absolute path")
    try:
        _no_reparse_components(config)
        config = config.resolve(strict=True)
        if not config.is_file() or config.suffix.lower() != ".toml":
            raise WindowsCloudflareConfigurationError("native ServerFS config is invalid")
        from .native_config import load_native_config

        workdirs, _settings = load_native_config(config)
        if not workdirs:
            raise WindowsCloudflareConfigurationError(
                "Cloudflare requires at least one configured workdir"
            )
        roots = []
        for entry in workdirs:
            root = Path(entry.root)
            _no_reparse_components(root)
            roots.append(root.resolve(strict=True))
        checkout = Path(__file__).resolve().parents[2]
        token = validate_private_file(
            token_file, checkout_root=checkout, workdir_roots=roots, user_sid=user_sid
        )
        env = validate_private_file(
            env_file, checkout_root=checkout, workdir_roots=roots, user_sid=user_sid
        )
        from .windows_service import _read_env_file

        origin_environment = _validate_origin_environment(_read_env_file(env), hostname)

        binary = Path(cloudflared).expanduser()
        expected = (
            serverfs_data_dir() / "bin" / f"cloudflared-{VERSION}-windows-amd64" / "cloudflared.exe"
        )
        if not binary.is_absolute() or not expected.is_absolute():
            raise WindowsCloudflareConfigurationError("cloudflared path must be absolute")
        _no_reparse_components(binary)
        binary = binary.resolve(strict=True)
        if binary != expected.resolve(strict=True) or not binary.is_file():
            raise WindowsCloudflareConfigurationError(
                "cloudflared must be the pinned user-level executable"
            )
        if _sha256(binary) != WINDOWS_BINARY_SHA256:
            raise WindowsCloudflareConfigurationError("cloudflared executable digest mismatch")
        _verify_windows_binary(binary)
    except WindowsCloudflareConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001 - credential boundary fails closed
        raise WindowsCloudflareConfigurationError(
            f"Cloudflare validation failed ({type(exc).__name__})"
        ) from exc
    return CloudflareWindowsSpec(
        config_path=config,
        cloudflared=binary,
        token_file=token,
        env_file=env,
        port=port,
        hostname=hostname,
        origin_environment=origin_environment,
    )
