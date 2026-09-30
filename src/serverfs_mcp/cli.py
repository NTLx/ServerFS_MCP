"""serverfs CLI skeleton (v0.10 Phase A, §26).

Minimal stdlib-argparse command line for native deployments:

    serverfs serve --config serverfs.toml
    serverfs doctor --config serverfs.toml

Phase A provides config parsing, startup wiring and the doctor skeleton on
the Linux platform only; the native stdio transport and Windows backend
arrive in later phases. ``serve`` intentionally refuses to start a native
listener until the platform dispatch exists — a half-wired native service
would silently run the wrong filesystem kernel.

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
    """Serve entry point: parse config, then run the platform dispatch.

    Phase A stops after parsing: the native stdio transport (Phase E) and
    the Windows backend (Phase B+) do not exist yet, and silently serving
    the Linux streamable-HTTP topology from a native config would be a
    different, undocumented deployment.
    """
    workdirs, server_settings = _load(args.config)
    jsonlog.set_level(server_settings.log_level)
    jsonlog.info(
        "native_serve_requested",
        workdirs=len(workdirs),
        read_write_workdirs=sum(1 for w in workdirs if not w.read_only),
        platform=sys.platform,
    )
    return _fail(
        "native serve is not available in Phase A; use the Linux Docker deployment (compose.yml)"
    )


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
    print("backend: linux (Phase A seam; no platform dispatch yet)", file=sys.stderr)
    for wd in workdirs:
        access = "read-write" if not wd.read_only else "read-only"
        print(f"workdir: {wd.alias} ({access})", file=sys.stderr)
    print(
        "doctor: config-level checks only in Phase A (root probes: not implemented)",
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
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
