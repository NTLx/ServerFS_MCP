"""Native ServerFS CLI (v0.10, §26).

Minimal stdlib-argparse command line for native deployments:

    serverfs serve --config serverfs.toml
    serverfs doctor --config serverfs.toml

Native serving uses the same MCP tools and platform backend dispatch as the
Docker service. Agent Bridge and file ingress remain disabled in this native
profile.

Output discipline (§27): MCP frames only on stdout; all structured logs go
to stderr. Doctor prints a human report to stderr so stdout stays reserved
for protocol use even in diagnostic runs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import SERVER_VERSION
from . import logging as jsonlog
from .native_config import NativeConfigError, load_native_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serverfs",
        description="ServerFS native MCP filesystem service",
    )
    parser.add_argument("--version", action="version", version=f"serverfs {SERVER_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the ServerFS MCP service (native profile)")
    serve.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to serverfs.toml (operator configuration; never agent input)",
    )

    doctor = sub.add_parser("doctor", help="Report configuration/platform diagnostics (read-only)")
    doctor.add_argument("--config", required=True, type=Path, help="Path to serverfs.toml")

    tunnel = sub.add_parser("tunnel", help="Run the official tunnel-client with native ServerFS")
    tunnel.add_argument("--config", required=True, type=Path, help="Path to serverfs.toml")
    tunnel.add_argument(
        "--env-file", type=Path, help="Proxy settings file (default: sibling .env, if present)"
    )
    tunnel.add_argument(
        "--tunnel-client", required=True, type=Path, help="Path to official tunnel-client.exe"
    )
    tunnel.add_argument("--tunnel-id", required=True, help="OpenAI tunnel_... identifier")
    tunnel.add_argument("--api-key-file", required=True, type=Path, help="Control-plane key file")
    return parser


def _fail(message: str) -> int:
    sys.stderr.write(f"serverfs: {message}\n")
    jsonlog.error("cli_failed", reason=message)
    return 2


def _load(path: Path):
    try:
        return load_native_config(path)
    except NativeConfigError as exc:
        raise SystemExit(_fail(f"configuration error: {exc}")) from exc


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the shared MCP tool registration over native stdio."""
    if sys.platform != "win32":
        return _fail("native serve is supported only on Windows")
    workdirs, native_settings = _load(args.config)
    from .config import Settings
    from .main import create_server
    from .workdirs import WorkdirRegistry

    settings = Settings(log_level=native_settings.log_level)
    registry = WorkdirRegistry(workdirs)
    jsonlog.set_level(settings.log_level)
    jsonlog.info(
        "native_serve_requested",
        workdirs=len(workdirs),
        read_write_workdirs=sum(1 for w in workdirs if not w.read_only),
        platform=sys.platform,
    )
    server = create_server(settings, registry)
    server.run("stdio")
    return 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    from .native_tunnel import run_native_tunnel

    try:
        return run_native_tunnel(
            config_path=args.config,
            env_file=args.env_file,
            tunnel_client=args.tunnel_client,
            tunnel_id=args.tunnel_id,
            api_key_file=args.api_key_file,
        )
    except (OSError, ValueError) as exc:
        return _fail(str(exc))


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only diagnostics: parse the config and report each workdir.

    Deliberately prints only alias/access/root-absolute facts to stderr.
    It does not touch any file inside a workdir: Phase A doctor reports
    configuration health, and the backend capability probes (root open,
    reparse status, filesystem type) arrive with the platform backends.
    """
    print(f"ServerFS {SERVER_VERSION}", file=sys.stderr)
    print(f"Python {sys.version.split()[0]} ({sys.platform})", file=sys.stderr)
    try:
        workdirs, _server = load_native_config(args.config)
    except NativeConfigError as exc:
        print(f"config: FAIL — {exc}", file=sys.stderr)
        return 2
    print(f"config: OK ({args.config})", file=sys.stderr)
    backend = {"win32": "windows", "linux": "linux"}.get(sys.platform, "unsupported")
    print(f"backend: {backend}", file=sys.stderr)
    for wd in workdirs:
        access = "read-write" if not wd.read_only else "read-only"
        print(f"workdir: {wd.alias} ({access})", file=sys.stderr)
    print(
        "doctor: config-level checks only (root probes: not implemented)",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "tunnel":
        return cmd_tunnel(args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
