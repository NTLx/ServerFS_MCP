"""Linux operator CLI.

Docker Compose owns Core/Connector lifecycle; the host Bridge uses systemd.
Legacy native-only arguments remain parseable for a clear refusal, never for
dispatch to another platform's implementation. Doctor and diagnostics retain
their existing output and authorization boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import SERVER_VERSION
from . import logging as jsonlog


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
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="Native transport (default: stdio)",
    )
    serve.add_argument("--port", "--http-port", dest="port", type=_http_port_arg, default=8765)
    serve.add_argument("--path", type=_http_path_arg, default="/mcp")
    serve.add_argument(
        "--macos-instance",
        default=None,
        help="macOS only: explicit named owned Core identity, verified against private manifest",
    )
    serve.add_argument(
        "--macos-shared-bridge",
        default=None,
        help="macOS only: explicitly bind a named Core to an authenticated shared Bridge",
    )
    serve.add_argument(
        "--macos-agent-protocol",
        choices=("v2",),
        default=None,
        help="macOS only: explicitly use signed-v2 Agent IPC for a staged owned Core",
    )

    doctor = sub.add_parser("doctor", help="Report configuration/platform diagnostics (read-only)")
    doctor.add_argument("--config", required=True, type=Path, help="Path to serverfs.toml")
    doctor.add_argument(
        "--env-file", type=Path, help="Proxy settings file (default: sibling .env, if present)"
    )
    doctor.add_argument(
        "--tunnel-client", type=Path, help="tunnel-client.exe to probe (default: bootstrap dir)"
    )

    diagnostics = sub.add_parser("diagnostics", help="Read local Agent Bridge diagnostics")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    for name in ("snapshot", "task"):
        diagnostic = diagnostics_sub.add_parser(name)
        diagnostic.add_argument(
            "--format", choices=("json", "human"), default="human", dest="diagnostics_format"
        )
        if name == "task":
            diagnostic.add_argument("task_id")

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
    tunnel.add_argument(
        "--api-key-file",
        type=Path,
        help=(
            "Control-plane key file (Windows flow). On macOS the key may instead be "
            "set as CONTROL_PLANE_API_KEY in the environment (.env)"
        ),
    )
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
        "bootstrap", help="Download/verify connectivity artifacts (never touches PATH)"
    )
    bootstrap_sub = bootstrap.add_subparsers(dest="bootstrap_target", required=True)
    bt = bootstrap_sub.add_parser(
        "tunnel-client",
        help="Install an official tunnel-client under the ServerFS data dir",
    )
    bt.add_argument(
        "--version",
        help="Official release tag; defaults to the ServerFS-tested version",
    )
    bt.add_argument(
        "--manifest-sha256",
        help=(
            "SHA-256 of that release's official SHA256SUMS.txt; required when --version "
            "differs from the ServerFS-tested version"
        ),
    )
    bt.add_argument("--force", action="store_true", help="Reinstall over an existing bootstrap")
    bw = bootstrap_sub.add_parser(
        "native-wheel", help="Download and verify the published Windows native wheel"
    )
    bw.add_argument("--url", required=True, help="Exact https release URL of the .whl")
    bw.add_argument("--sha256", required=True, help="Release-recorded SHA-256 of the wheel")
    bc = bootstrap_sub.add_parser(
        "cloudflare-client", help="Install pinned official macOS/Windows cloudflared"
    )
    bc.add_argument(
        "--artifact", type=Path, help="Local official release archive (for offline use)"
    )

    agent = sub.add_parser(
        "agent-bridge",
        help="Manage the macOS user LaunchAgent for the Agent Bridge (v0.13, darwin only)",
    )
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    ai = agent_sub.add_parser("install", help="Generate the LaunchAgent plist and bootstrap it")
    ai.add_argument("--bridge-config", required=True, type=Path, help="Bridge JSON config path")
    ai.add_argument(
        "--bridge-executable",
        type=Path,
        help="serverfs-agent-bridge executable (default: beside the active interpreter)",
    )
    ai.add_argument("--log-dir", type=Path, help="Bridge stdout/stderr log directory")
    ai.add_argument("--force", action="store_true", help="Reinstall over an existing plist")
    configure = agent_sub.add_parser(
        "configure",
        help="Create/update the private Bridge config from serverfs.toml and private env values",
    )
    configure.add_argument("--config", required=True, type=Path, help="native serverfs.toml path")
    configure.add_argument(
        "--env-file",
        type=Path,
        help="private Agent/Jev/proxy settings file (default: current process environment)",
    )
    agent_sub.add_parser("start", help="Kickstart the LaunchAgent")
    ar = agent_sub.add_parser(
        "restart",
        help="Optionally synchronize serverfs.toml policy, then kickstart -k the LaunchAgent",
    )
    ar.add_argument(
        "--config",
        type=Path,
        help="serverfs.toml to synchronize into the private Bridge config before restart",
    )
    ar.add_argument(
        "--env-file",
        type=Path,
        help="also refresh private Agent/Jev/proxy settings from this file before restart",
    )
    ar.add_argument("--_handover-child", action="store_true", help=argparse.SUPPRESS)
    async_parser = agent_sub.add_parser(
        "sync",
        help="Synchronize serverfs.toml-owned policy into the private Bridge config",
    )
    async_parser.add_argument(
        "--config", required=True, type=Path, help="native serverfs.toml path"
    )
    agent_sub.add_parser("stop", help="Stop the persistent Agent Bridge service")
    agent_sub.add_parser("status", help="Read the persistent Agent Bridge service status")
    agent_sub.add_parser("uninstall", help="Unregister the persistent Agent Bridge service")

    service = sub.add_parser(
        "service",
        help="Manage platform-native persistent ServerFS services (macOS / Windows)",
    )
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_install = service_sub.add_parser(
        "install",
        help="Build/install the native Service Manager and register Tunnel/Agent Bridge",
    )
    service_install.add_argument(
        "--config", required=True, type=Path, help="native serverfs.toml path"
    )
    service_install.add_argument(
        "--env-file",
        type=Path,
        help="private service settings (default: sibling .env)",
    )
    service_install.add_argument(
        "--_handover-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    service_install.add_argument(
        "--_handover-roots",
        help=argparse.SUPPRESS,
    )
    service_status = service_sub.add_parser("status", help="Report registration and process health")
    service_status.add_argument(
        "--format",
        dest="status_format",
        choices=("native", "common-json", "common-human"),
        default="native",
    )
    service_start = service_sub.add_parser("start", help="Register persistent services")
    service_restart = service_sub.add_parser("restart", help="Restart persistent services")
    service_restart.add_argument("--_handover-child", action="store_true", help=argparse.SUPPRESS)
    service_stop = service_sub.add_parser("stop", help="Unregister persistent services")
    for operation in (service_start, service_restart, service_stop):
        operation.add_argument(
            "--component",
            choices=("tunnel", "cloudflare", "core"),
            help="macOS component-only lifecycle; preserves other components",
        )
    service_sub.add_parser(
        "uninstall",
        help="Unregister services and remove platform-owned persistent lifecycle artifacts",
    )
    service_sub.add_parser("open-settings", help="Open System Settings > Login Items (macOS)")
    core = service_sub.add_parser(
        "core", help="Stage and inspect the independently managed macOS HTTP Core"
    )
    core_sub = core.add_subparsers(dest="core_command", required=True)
    core_stage = core_sub.add_parser(
        "stage", help="Validate and stage Core config without registering or starting it"
    )
    core_stage.add_argument("--config", required=True, type=Path)
    core_stage.add_argument("--env-file", required=True, type=Path)
    core_stage.add_argument("--http-port", required=True, type=_http_port_arg)
    core_stage.add_argument("--hostname", required=True)
    core_sub.add_parser("status")
    core_sub.add_parser("unstage", help="Remove only an unregistered staged Core definition")
    openai = service_sub.add_parser(
        "openai", help="Stage an independent macOS OpenAI Tunnel HTTP-to-Core connector"
    )
    openai_sub = openai.add_subparsers(dest="openai_command", required=True)
    openai_stage = openai_sub.add_parser(
        "stage", help="Stage direct HTTP-to-Core Tunnel without registering it"
    )
    openai_stage.add_argument("--config", required=True, type=Path)
    openai_stage.add_argument("--tunnel-client", required=True, type=Path)
    openai_stage.add_argument("--tunnel-id", required=True)
    openai_stage.add_argument("--control-plane-key-file", required=True, type=Path)
    openai_stage.add_argument("--core-bearer-file", required=True, type=Path)
    openai_sub.add_parser(
        "unstage", help="Remove an unregistered independent OpenAI HTTP connector"
    )
    cloudflare = service_sub.add_parser(
        "cloudflare", help="Manage opt-in macOS or Windows Cloudflare HTTP ingress"
    )
    cloudflare_sub = cloudflare.add_subparsers(dest="cloudflare_command", required=True)
    cloudflare_install = cloudflare_sub.add_parser("install")
    cloudflare_install.add_argument("--config", required=True, type=Path)
    cloudflare_install.add_argument("--cloudflared", required=True, type=Path)
    cloudflare_install.add_argument("--token-file", required=True, type=Path)
    cloudflare_install.add_argument("--env-file", required=True, type=Path)
    cloudflare_install.add_argument("--http-port", required=True, type=_http_port_arg)
    cloudflare_install.add_argument("--hostname", required=True)
    cloudflare_stage = cloudflare_sub.add_parser(
        "stage", help="Stage one macOS connector for an independently staged Core"
    )
    cloudflare_stage.add_argument("--config", required=True, type=Path)
    cloudflare_stage.add_argument("--cloudflared", required=True, type=Path)
    cloudflare_stage.add_argument("--token-file", required=True, type=Path)
    cloudflare_stage.add_argument("--hostname", required=True)
    cloudflare_sub.add_parser(
        "unstage", help="Remove only an unregistered independently staged macOS connector"
    )
    for lifecycle in ("status", "start", "stop", "restart", "uninstall"):
        cloudflare_sub.add_parser(lifecycle)
    parallel = service_sub.add_parser(
        "cloudflare-parallel",
        help="Manage the independent second Windows Cloudflare instance (WorkPC)",
    )
    parallel_sub = parallel.add_subparsers(dest="parallel_command", required=True)
    parallel_install = parallel_sub.add_parser("install")
    parallel_install.add_argument("--config", required=True, type=Path)
    parallel_install.add_argument("--cloudflared", required=True, type=Path)
    parallel_install.add_argument("--token-file", required=True, type=Path)
    parallel_install.add_argument("--env-file", required=True, type=Path)
    for lifecycle in ("status", "start", "stop", "restart", "uninstall"):
        parallel_sub.add_parser(lifecycle)
    for internal in ("_watchdog", "_run"):
        parallel_internal = parallel_sub.add_parser(internal, help=argparse.SUPPRESS)
        parallel_internal.add_argument("--config", required=True, type=Path, help=argparse.SUPPRESS)
        parallel_internal.add_argument(
            "--cloudflared", required=True, type=Path, help=argparse.SUPPRESS
        )
        parallel_internal.add_argument(
            "--token-file", required=True, type=Path, help=argparse.SUPPRESS
        )
        parallel_internal.add_argument(
            "--env-file", required=True, type=Path, help=argparse.SUPPRESS
        )
    service_watchdog = service_sub.add_parser("_watchdog", help=argparse.SUPPRESS)
    service_watchdog.add_argument("--config", required=True, type=Path, help=argparse.SUPPRESS)
    service_watchdog.add_argument("--env-file", required=True, type=Path, help=argparse.SUPPRESS)
    service_run = service_sub.add_parser("_run", help=argparse.SUPPRESS)
    service_run.add_argument("--config", required=True, type=Path, help=argparse.SUPPRESS)
    service_run.add_argument("--env-file", required=True, type=Path, help=argparse.SUPPRESS)
    service_sub.add_parser("_handover-cleanup", help=argparse.SUPPRESS)
    return parser


def _fail(message: str) -> int:
    sys.stderr.write(f"serverfs: {message}\n")
    jsonlog.error("cli_failed", reason=message)
    return 2


def _http_port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def _http_path_arg(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(char.isspace() for char in value)
        or "?" in value
        or "#" in value
    ):
        raise argparse.ArgumentTypeError("path must be an absolute URL path without query/fragment")
    return value


def cmd_serve(args: argparse.Namespace, *, agent_wiring_override=None) -> int:
    return _fail("native serve is supported only on Windows and macOS 27 (Apple Silicon)")


def cmd_tunnel(args: argparse.Namespace) -> int:
    return _fail(
        "the Linux deployment runs the Docker compose topology; the native "
        "tunnel launcher is supported on Windows and macOS 27"
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(
        args.config,
        env_file=args.env_file,
        tunnel_client=args.tunnel_client,
        writer=lambda line: print(line, file=sys.stderr),
    )


def _diagnostics_human(value: dict) -> str:
    lines: list[str] = []

    def visit(item, prefix: str) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                visit(item[key], f"{prefix}.{key}" if prefix else key)
        elif isinstance(item, list):
            lines.append(f"{prefix}: {json.dumps(item, ensure_ascii=False, separators=(',', ':'))}")
        else:
            lines.append(f"{prefix}: {json.dumps(item, ensure_ascii=False)}")

    visit(value, "")
    return "\n".join(lines)


def cmd_diagnostics(args: argparse.Namespace) -> int:
    from .agent_client import AgentBridgeClient, AgentBridgeUnavailable

    endpoint = os.environ.get(
        "SERVERFS_AGENT_BRIDGE_SOCKET", "/run/serverfs-agent-bridge/bridge.sock"
    ).strip()
    if not endpoint:
        endpoint = "/run/serverfs-agent-bridge/bridge.sock"
    method = (
        "diagnostics.snapshot" if args.diagnostics_command == "snapshot" else "diagnostics.task"
    )
    params = {} if method.endswith("snapshot") else {"task_id": args.task_id}
    try:
        result = asyncio.run(AgentBridgeClient(Path(endpoint)).call(method, params))
    except AgentBridgeUnavailable:
        result = {"catalog_version": 1, "disposition": "bridge_unavailable"}
    if args.diagnostics_format == "json":
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(_diagnostics_human(result))
    return 1 if result.get("disposition") == "bridge_unavailable" else 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .tunnel_bootstrap import (
        TESTED_TUNNEL_CLIENT_VERSION,
        BootstrapError,
        bootstrap_tunnel_client,
    )

    try:
        if args.bootstrap_target == "cloudflare-client":
            return _fail("Linux Cloudflare Connector is provided by Docker Compose")
        if args.bootstrap_target == "tunnel-client":
            path = bootstrap_tunnel_client(
                force=args.force,
                version=args.version or TESTED_TUNNEL_CLIENT_VERSION,
                pinned_manifest_sha256=args.manifest_sha256,
            )
            sys.stderr.write(f"tunnel-client installed: {path}\n")
            return 0
        if args.bootstrap_target == "native-wheel":
            return _fail("native-wheel is Windows-only; Linux uses the built-in fdio backend")
    except BootstrapError as exc:
        return _fail(f"bootstrap failed: {exc}")
    return _fail(f"unknown bootstrap target {args.bootstrap_target!r}")


def cmd_agent_bridge(args: argparse.Namespace) -> int:
    return _fail("agent-bridge lifecycle management is macOS-only in v0.13")


def cmd_service(args: argparse.Namespace) -> int:
    return _fail("persistent native service management is unsupported on Linux; use Docker Compose")


def main(argv: list[str] | None = None, *, agent_wiring_override=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args, agent_wiring_override=agent_wiring_override)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "diagnostics":
        return cmd_diagnostics(args)
    if args.command == "tunnel":
        return cmd_tunnel(args)
    if args.command == "bootstrap":
        return cmd_bootstrap(args)
    if args.command == "agent-bridge":
        return cmd_agent_bridge(args)
    if args.command == "service":
        return cmd_service(args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
