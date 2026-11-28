"""Independent Windows MCP HTTP Core entrypoint (v0.25.0 W1c source).

Task Scheduler should directly supervise this long-running Python process.
No Agent Bridge, Tunnel, connector or nested subprocess is started here.
The existing native CLI/SDK implements all Core MCP tools and HTTP sessions.

Source-only: no Task Scheduler task is installed or started by this module.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .windows_cloudflare_config import (
    _LABEL,
    WindowsCloudflareConfigurationError,
    _port_available,
    _validate_origin_environment,
)
from .windows_cloudflare_security import WindowsPrivateFileError, validate_private_file

_AUTH_KEYS = frozenset(
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


class WindowsCoreError(Exception):
    """Fixed, redacted Windows Core service refusal."""


@dataclass(frozen=True)
class CoreRuntimeSpec:
    config_path: Path
    auth_file: Path
    port: int
    hostname: str
    user_sid: str


def _ensure_windows() -> None:
    if sys.platform != "win32" or os.name != "nt":
        raise WindowsCoreError("independent Windows Core requires Windows")


def _strict_hostname(hostname: str) -> str:
    if (
        not isinstance(hostname, str)
        or not 1 <= len(hostname) <= 253
        or any(_LABEL.fullmatch(label) is None for label in hostname.split("."))
    ):
        raise WindowsCoreError("Core requires exactly one valid HTTP host")
    return hostname


def _verified_config(config_path: Path) -> tuple[Path, list[Path]]:
    """Resolve the existing native policy and its complete exposed root set."""
    from .native_config import load_native_config
    from .windows_cloudflare_security import _no_reparse_components

    if not config_path.is_absolute():
        raise WindowsCoreError("Core native config must be absolute")
    try:
        _no_reparse_components(config_path)
        resolved = config_path.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.casefold() != ".toml":
            raise WindowsCoreError("Core native policy file is invalid")
        workdirs, _settings = load_native_config(resolved)
        if not workdirs:
            raise WindowsCoreError("Core requires at least one configured workdir")
        roots: list[Path] = []
        for workdir in workdirs:
            _no_reparse_components(workdir.root)
            roots.append(workdir.root.resolve(strict=True))
        return resolved, roots
    except WindowsCoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not leak config or private user paths
        raise WindowsCoreError("Core native policy could not be validated") from exc


def _read_auth_file(path: Path, hostname: str, port: int) -> dict[str, str]:
    """Validate exactly two trusted Host headers and one existing auth mode.

    Cloudflare forwards the public Host; OpenAI HTTP connects to the Core
    loopback URL. Both must be explicit, not wildcard/implicit aliases.
    """
    from .windows_service import _read_env_file

    try:
        values = _read_env_file(path)
        if set(values) - _AUTH_KEYS:
            raise WindowsCoreError("Core auth file contains unsupported settings")
        hosts = values.get("SERVERFS_HTTP_ALLOWED_HOSTS", "")
        parts = hosts.split(",")
        expected = {hostname, f"127.0.0.1:{port}"}
        if len(parts) != 2 or set(parts) != expected:
            raise WindowsCoreError("Core HTTP Host allowlist must match both ingress paths")
        # Reuse the already-reviewed single-provider auth/OAuth validator
        # without narrowing the actual Core Host allowlist passed to the CLI.
        single = {**values, "SERVERFS_HTTP_ALLOWED_HOSTS": hostname}
        _validate_origin_environment(single, hostname)
        return dict(values)
    except WindowsCoreError:
        raise
    except (OSError, ValueError, WindowsCloudflareConfigurationError) as exc:
        raise WindowsCoreError("Core HTTP authentication settings are invalid") from exc


def validate_runtime(
    spec: CoreRuntimeSpec, *, require_free_port: bool = False
) -> tuple[Path, Path, dict[str, str]]:
    """Read-only preflight. No secrets are returned to callers via status/logs."""
    _ensure_windows()
    if type(spec.port) is not int or not 1 <= spec.port <= 65535:
        raise WindowsCoreError("Core loopback port is invalid")
    hostname = _strict_hostname(spec.hostname)
    config, roots = _verified_config(spec.config_path)
    from .native_endpoint import current_user_sid

    if spec.user_sid != current_user_sid():
        raise WindowsCoreError("Core service caller SID does not match the current token")
    from .windows_cloudflare_security import _no_reparse_components

    checkout = Path(__file__).resolve().parents[2]
    try:
        _no_reparse_components(checkout)
        auth_file = validate_private_file(
            spec.auth_file,
            checkout_root=checkout,
            workdir_roots=roots,
            user_sid=spec.user_sid,
        )
    except WindowsPrivateFileError as exc:
        raise WindowsCoreError("Core HTTP authentication file is not private") from exc
    auth = _read_auth_file(auth_file, hostname, spec.port)
    if require_free_port and not _port_available(spec.port):
        raise WindowsCoreError("Core loopback port is already allocated")
    return config, auth_file, auth


def _core_environment(
    auth: Mapping[str, str], *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Explicit allowlist: no ambient Tunnel credentials, proxy or Agent launch keys."""
    original = os.environ if source is None else source
    # Preserve only Windows' minimal process identity and native loading keys.
    essentials = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
    }
    base = {key: value for key, value in original.items() if key.upper() in essentials}
    from .native_lifecycle import window_local_app_data

    # The inherited LOCALAPPDATA string is not a Windows identity source.
    # Bind Core-side lock/data derivation to the actual current-user folder.
    base["LOCALAPPDATA"] = str(window_local_app_data())
    if set(auth) - _AUTH_KEYS:
        raise WindowsCoreError("Core environment has unsupported authentication fields")
    return {**base, **dict(auth)}


def run_core(spec: CoreRuntimeSpec) -> int:
    """Run the existing HTTP Core in this process; Bridge is only an IPC peer."""
    config, _auth_file, auth = validate_runtime(spec, require_free_port=True)
    # A parsed config and a protected key are insufficient: a user process
    # could otherwise serve as an unregistered Core and confuse connectors.
    from .windows_core_control import core_owner_mutex, record_runtime_owner

    # The lock belongs solely to Core; stopping any ingress cannot release
    # it. Hold it until the MCP SDK's HTTP server actually exits.
    with core_owner_mutex():
        record_runtime_owner(spec)
        child_env = _core_environment(auth)
        # No child Core, supervisor, ingress connector or Agent Provider.
        os.environ.clear()
        os.environ.update(child_env)
        from . import cli

        return cli.main(
            [
                "serve",
                "--config",
                str(config),
                "--transport",
                "streamable-http",
                "--http-port",
                str(spec.port),
            ]
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serverfs-windows-core")
    parser.add_argument("_mode", choices=("_run",))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--auth-file", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--hostname", required=True)
    args = parser.parse_args(argv)
    try:
        from .native_endpoint import current_user_sid

        return run_core(
            CoreRuntimeSpec(
                config_path=args.config,
                auth_file=args.auth_file,
                port=args.port,
                hostname=args.hostname,
                user_sid=current_user_sid(),
            )
        )
    except WindowsCoreError as exc:
        print(f"serverfs-windows-core: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never surface source path / secrets
        print(
            f"serverfs-windows-core: operation failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
