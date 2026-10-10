"""Native ServerFS CLI (v0.10, §26).

Minimal stdlib-argparse command line for native deployments:

    serverfs serve --config serverfs.toml
    serverfs doctor --config serverfs.toml
    serverfs tunnel --config serverfs.toml --tunnel-id tunnel_... --api-key-file KEY
    serverfs bootstrap tunnel-client | native-wheel

Native serving uses the same MCP tools and platform backend dispatch as the
Docker service. Agent Bridge and file ingress remain disabled in this native
profile.

Output discipline (§27): MCP frames only on stdout; all structured logs go
to stderr. Doctor and bootstrap diagnostics print to stderr so stdout stays
reserved for protocol use even in diagnostic runs.
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
    doctor.add_argument(
        "--env-file", type=Path, help="Proxy settings file (default: sibling .env, if present)"
    )
    doctor.add_argument(
        "--tunnel-client", type=Path, help="tunnel-client.exe to probe (default: bootstrap dir)"
    )

    tunnel = sub.add_parser("tunnel", help="Run the official tunnel-client with native ServerFS")
    tunnel.add_argument("--config", required=True, type=Path, help="Path to serverfs.toml")
    tunnel.add_argument(
        "--env-file", type=Path, help="Proxy settings file (default: sibling .env, if present)"
    )
    tunnel.add_argument(
        "--tunnel-client",
        type=Path,
        help="Path to official tunnel-client.exe (default: bootstrapped copy)",
    )
    tunnel.add_argument("--tunnel-id", required=True, help="OpenAI tunnel_... identifier")
    tunnel.add_argument("--api-key-file", required=True, type=Path, help="Control-plane key file")
    tunnel.add_argument(
        "--base-url",
        help="Override the https control-plane endpoint (acceptance/ops use; default upstream)",
    )
    tunnel.add_argument(
        "--health-listen-addr",
        help=(
            "tunnel-client health bind: 127.0.0.1:<port 0..65535> only "
            "(default 127.0.0.1:0, an ephemeral loopback port, because 8080 "
            "collides with common local services)"
        ),
    )

    bootstrap = sub.add_parser(
        "bootstrap", help="Download/verify pinned connectivity artifacts (never touches PATH)"
    )
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_target", required=True)
    bt = bootstrap_sub.add_parser(
        "tunnel-client",
        help="Install the pinned official tunnel-client under the ServerFS data dir",
    )
    bt.add_argument("--force", action="store_true", help="Reinstall over an existing bootstrap")
    bw = bootstrap_sub.add_parser(
        "native-wheel", help="Download and verify the published Windows native wheel"
    )
    bw.add_argument("--url", required=True, help="Exact https release URL of the .whl")
    bw.add_argument("--sha256", required=True, help="Release-recorded SHA-256 of the wheel")
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


def _native_agent_settings(native_settings) -> tuple | None:
    """Build Agent wiring for a native serve, or ``None`` when delegation is off.

    ``serverfs.toml`` is the only operator-facing source of Agent policy: this function is gated on
    ``native_settings.agent_enabled`` and never on an ambient environment variable, so a stray
    ``SERVERFS_AGENT_BRIDGE_ENABLED`` in a shell cannot turn Agent delegation on.

    The endpoint and lock directory are **derived**, not required from the environment. A direct
    ``serverfs serve`` is not a supervisor launcher. The Bridge lifecycle belongs to the
    supervisor, so serve starts none, but it must still address one and must reach the same
    address a supervised serve would without being told. The supervisor may inject the same values
    as defence in depth; injecting *different* ones is a startup refusal, because two derivation
    paths producing two addresses would put a supervisor and a direct serve in different pipe and
    lease universes.

    With nothing listening, the ten Agent tools are still registered and calls fail closed through
    the frozen ``AgentBridgeUnavailable``. That is §15 D3's requirement, and it is why registration
    does not depend on a running Bridge.
    """
    from .config import Settings

    if not native_settings.agent_enabled:
        return None

    from .native_endpoint import NativeEndpointError, resolve_native_agent_wiring

    try:
        endpoint, lock_dir = resolve_native_agent_wiring()
    except NativeEndpointError as exc:
        raise SystemExit(_fail(f"agent endpoint unavailable: {exc}")) from exc

    settings = Settings(
        log_level=native_settings.log_level,
        agent_bridge_enabled=True,
        agent_bridge_socket=endpoint,
        agent_lock_dir=str(lock_dir),
    )
    from .agent_client import AgentBridgeClient

    client = AgentBridgeClient(Path(endpoint), timeout_seconds=30.0)
    return settings, client


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the shared MCP tool registration over native stdio."""
    if sys.platform == "darwin":
        # v0.13 macOS gate: M-series / native arm64 / macOS 27 / not Rosetta
        # (dev_plan_v0.13.md §2.1). Any other Darwin environment refuses
        # loudly instead of silently executing.
        from .darwin_platform import UNSUPPORTED_CODE, darwin_platform_status

        status = darwin_platform_status()
        if not status.supported:
            return _fail(f"{UNSUPPORTED_CODE}: {status.reason}")
    elif sys.platform != "win32":
        return _fail("native serve is supported only on Windows and macOS 27 (Apple Silicon)")
    workdirs, native_settings = _load(args.config)
    from .config import Settings
    from .main import create_server
    from .workdirs import WorkdirRegistry

    agent_wiring = _native_agent_settings(native_settings)
    if agent_wiring is None:
        settings = Settings(log_level=native_settings.log_level)
        client = None
    else:
        settings, client = agent_wiring
    registry = WorkdirRegistry(workdirs)
    jsonlog.set_level(settings.log_level)
    jsonlog.info(
        "native_serve_requested",
        workdirs=len(workdirs),
        read_write_workdirs=sum(1 for w in workdirs if not w.read_only),
        platform=sys.platform,
        agent_bridge_enabled=settings.agent_bridge_enabled,
    )
    server = create_server(settings, registry, client)
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
            base_url=args.base_url,
            health_listen_addr=args.health_listen_addr,
        )
    except (OSError, ValueError) as exc:
        return _fail(str(exc))


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(
        args.config,
        env_file=args.env_file,
        tunnel_client=args.tunnel_client,
        writer=lambda line: print(line, file=sys.stderr),
    )


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .tunnel_bootstrap import BootstrapError, bootstrap_native_wheel, bootstrap_tunnel_client

    try:
        if args.bootstrap_target == "tunnel-client":
            if sys.platform == "darwin":
                # v0.13 ships and accepts only the darwin-arm64 official
                # asset; refuse anything else before downloading (C3).
                from .darwin_platform import UNSUPPORTED_CODE, darwin_platform_status

                status = darwin_platform_status()
                if not status.supported:
                    return _fail(
                        f"{UNSUPPORTED_CODE}: tunnel-client bootstrap requires native arm64 "
                        f"macOS 27 ({status.reason})"
                    )
            path = bootstrap_tunnel_client(force=args.force)
            sys.stderr.write(f"tunnel-client installed: {path}\n")
            return 0
        if args.bootstrap_target == "native-wheel":
            if sys.platform == "darwin":
                # C4: the native wheel stays Windows-only; macOS needs no
                # compiled kernel package, so refuse clearly instead of
                # creating a dummy artifact.
                return _fail(
                    "native-wheel is not required on macOS: the Darwin backend is pure "
                    "Python over POSIX/Darwin primitives (no native package exists for v0.13)"
                )
            path = bootstrap_native_wheel(url=args.url, sha256=args.sha256)
            sys.stderr.write(f"native wheel verified and stored: {path}\n")
            sys.stderr.write(
                "install into the active environment with: "
                f"uv pip install '{path}'  (or: python -m pip install '{path}')\n"
            )
            return 0
    except BootstrapError as exc:
        return _fail(f"bootstrap failed: {exc}")
    return _fail(f"unknown bootstrap target {args.bootstrap_target!r}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "tunnel":
        return cmd_tunnel(args)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
