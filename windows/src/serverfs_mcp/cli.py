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
to stderr. Doctor and bootstrap diagnostics print to stderr. The standalone
``diagnostics`` command writes its explicitly requested local report to stdout;
it is not registered with or reachable through the MCP server.
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


def _load(path: Path):
    try:
        return load_native_config(path)
    except NativeConfigError as exc:
        raise SystemExit(_fail(f"configuration error: {exc}")) from exc


def _native_agent_settings(
    native_settings, *, owned_wiring: tuple[str, Path] | None = None
) -> tuple | None:
    """Build native Bridge wiring, or ``None`` when Agent and Repo Ops are both off.

    ``serverfs.toml`` is the only operator-facing source of Bridge policy: this function is gated on
    ``native_settings.bridge_required`` and never on an ambient environment variable, so a stray
    ``SERVERFS_AGENT_BRIDGE_ENABLED`` in a shell cannot turn either capability on.

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

    if not native_settings.bridge_required:
        return None

    from .native_endpoint import NativeEndpointError, resolve_native_agent_wiring

    try:
        if owned_wiring is not None:
            if sys.platform != "darwin":
                raise NativeEndpointError("named owned wiring requires macOS")
            endpoint, lock_dir = owned_wiring
        else:
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


def _overlay_ingress_env(settings):
    """Return ``settings`` with file-ingress fields resolved from the env.

    File-ingress policy is an environment-domain setting (the MCP child owns
    the helper process), while the agent-wiring and log-level Settings are
    built directly by their own code paths — so the env fields are overlaid
    here on BOTH branches of ``cmd_serve``. Without this, a directly
    constructed Settings kept ``file_ingress_enabled=False`` and the helper
    never spawned even with SERVERFS_FILE_INGRESS_ENABLED=true (measured).
    """
    from dataclasses import replace

    from .config import settings_from_env

    env_settings = settings_from_env()
    if env_settings.file_ingress_enabled == settings.file_ingress_enabled and (
        env_settings.file_ingress_timeout_seconds == settings.file_ingress_timeout_seconds
    ):
        return settings
    return replace(
        settings,
        file_ingress_enabled=env_settings.file_ingress_enabled,
        file_ingress_timeout_seconds=env_settings.file_ingress_timeout_seconds,
        file_ingress_socket=env_settings.file_ingress_socket,
    )


def cmd_serve(args: argparse.Namespace, *, agent_wiring_override=None) -> int:
    """Run the shared MCP tool registration over native stdio."""
    if sys.platform != "win32":
        return _fail("native Windows serve requires Windows")
    workdirs, native_settings = _load(args.config)
    named_instance = getattr(args, "macos_instance", None)
    shared_bridge = getattr(args, "macos_shared_bridge", None)
    owned_agent_protocol = getattr(args, "macos_agent_protocol", None)
    if shared_bridge is not None and not named_instance:
        return _fail("--macos-shared-bridge requires explicit --macos-instance")
    if shared_bridge == "":
        return _fail("--macos-shared-bridge must be non-empty")
    if owned_agent_protocol is not None and (not named_instance or shared_bridge is not None):
        return _fail("--macos-agent-protocol v2 requires an explicitly named owned Core")
    if named_instance and shared_bridge:
        return _fail("named shared Bridge requires macOS Streamable HTTP")
    if named_instance and owned_agent_protocol == "v2":
        return _fail("named owned Bridge v2 requires macOS Streamable HTTP")
    if named_instance:
        return _fail("named owned Core requires macOS Streamable HTTP")
    from .config import Settings
    from .main import create_server
    from .workdirs import WorkdirRegistry

    if agent_wiring_override is not None:
        from .agent_core_grant_client import CoreGrantBridgeClient

        if (
            not native_settings.bridge_required
            or not isinstance(agent_wiring_override, tuple)
            or len(agent_wiring_override) != 2
            or not isinstance(agent_wiring_override[0], Settings)
            or not isinstance(agent_wiring_override[1], CoreGrantBridgeClient)
            or not agent_wiring_override[0].agent_bridge_enabled
        ):
            return _fail("native Bridge v2 Core owner wiring is invalid")
        agent_wiring = agent_wiring_override
    else:
        agent_wiring = _native_agent_settings(native_settings, owned_wiring=None)
    if agent_wiring is None:
        settings = Settings(log_level=native_settings.log_level)
        client = None
    else:
        settings, client = agent_wiring
    if args.transport == "streamable-http":
        from dataclasses import replace

        from .config import settings_from_env

        try:
            http_settings = settings_from_env(os.environ)
            if not http_settings.http_api_key and not http_settings.oauth_enabled:
                return _fail("Streamable HTTP requires SERVERFS_HTTP_API_KEY or configured OAuth")
            settings = replace(
                settings,
                http_host="127.0.0.1",
                http_port=args.port,
                http_path=args.path,
                http_allowed_hosts=http_settings.http_allowed_hosts,
                http_allowed_origins=(f"http://127.0.0.1:{args.port}",),
                http_api_key=http_settings.http_api_key,
                oauth_enabled=http_settings.oauth_enabled,
                oauth_resource_url=http_settings.oauth_resource_url,
                oauth_issuer_url=http_settings.oauth_issuer_url,
                oauth_required_scopes=http_settings.oauth_required_scopes,
                oauth_introspection_url=http_settings.oauth_introspection_url,
                oauth_introspection_secret=http_settings.oauth_introspection_secret,
                oauth_introspection_timeout_seconds=http_settings.oauth_introspection_timeout_seconds,
            )
        except ValueError as exc:
            return _fail(f"Streamable HTTP configuration error: {exc}")
    settings = _overlay_ingress_env(settings)
    file_ingress_client = None
    registry = WorkdirRegistry(workdirs)
    jsonlog.set_level(settings.log_level)
    jsonlog.info(
        "native_serve_requested",
        workdirs=len(workdirs),
        read_write_workdirs=sum(1 for w in workdirs if not w.read_only),
        platform=sys.platform,
        agent_bridge_enabled=settings.agent_bridge_enabled,
        file_ingress_enabled=file_ingress_client is not None,
    )
    if args.transport == "streamable-http":
        from .main import run_streamable_http

        try:
            server = create_server(settings, registry, client, file_ingress_client)
            run_streamable_http(server, settings, registry)
        except ValueError as exc:
            return _fail(f"Streamable HTTP configuration error: {exc}")
    else:
        server = create_server(settings, registry, client, file_ingress_client)
        server.run("stdio")
    return 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    from .native_tunnel import run_native_tunnel

    api_key = os.environ.get("CONTROL_PLANE_API_KEY", "").strip() or None
    try:
        return run_native_tunnel(
            config_path=args.config,
            env_file=args.env_file,
            tunnel_client=args.tunnel_client,
            tunnel_id=args.tunnel_id,
            api_key_file=args.api_key_file,
            api_key=api_key,
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
    from .native_endpoint import NativeEndpointError, resolve_native_agent_wiring

    if sys.platform.startswith("linux"):
        endpoint = os.environ.get(
            "SERVERFS_AGENT_BRIDGE_SOCKET", "/run/serverfs-agent-bridge/bridge.sock"
        ).strip()
        if not endpoint:
            endpoint = "/run/serverfs-agent-bridge/bridge.sock"
    else:
        try:
            endpoint, _lock_dir = resolve_native_agent_wiring()
        except NativeEndpointError as exc:
            return _fail(f"agent endpoint unavailable: {exc}")
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
        bootstrap_native_wheel,
        bootstrap_tunnel_client,
    )

    try:
        if args.bootstrap_target == "cloudflare-client":
            from .cloudflare_bootstrap import (
                VERSION,
                CloudflareBootstrapError,
                bootstrap_cloudflare_client,
            )

            try:
                path = bootstrap_cloudflare_client(artifact=args.artifact)
            except CloudflareBootstrapError as exc:
                return _fail(f"bootstrap failed: {exc}")
            sys.stderr.write(f"cloudflared {VERSION} installed: {path.resolve()}\n")
            return 0
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
            path = bootstrap_tunnel_client(
                force=args.force,
                version=args.version or TESTED_TUNNEL_CLIENT_VERSION,
                pinned_manifest_sha256=args.manifest_sha256,
            )
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


def cmd_agent_bridge(args: argparse.Namespace) -> int:
    return _fail("agent-bridge lifecycle management is macOS-only in v0.13")


def cmd_service(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        return _fail("persistent Windows service management requires Windows")
    command = args.service_command
    if command in {"openai", "core"}:
        return _fail("independent native staging command requires macOS")
    if command == "cloudflare-parallel":
        if sys.platform != "win32":
            return _fail("parallel Cloudflare service lifecycle requires Windows")
        from . import windows_cloudflare_parallel as parallel
        from .windows_cloudflare_parallel import WindowsCloudflareParallelError

        try:
            action = args.parallel_command
            if action == "install":
                snapshot = parallel.install_instance(
                    args.config,
                    args.cloudflared,
                    args.token_file,
                    args.env_file,
                )
                sys.stderr.write(json.dumps(snapshot, sort_keys=True) + "\n")
            elif action == "status":
                sys.stderr.write(json.dumps(parallel.status_instance(), sort_keys=True) + "\n")
            elif action == "start":
                sys.stderr.write(json.dumps(parallel.start_instance(), sort_keys=True) + "\n")
            elif action == "stop":
                sys.stderr.write(json.dumps(parallel.stop_instance(), sort_keys=True) + "\n")
            elif action == "restart":
                sys.stderr.write(json.dumps(parallel.restart_instance(), sort_keys=True) + "\n")
            elif action == "uninstall":
                parallel.uninstall_instance()
                sys.stderr.write("parallel Cloudflare instance uninstalled\n")
            elif action == "_watchdog":
                return parallel.run_watchdog(
                    args.config, args.cloudflared, args.token_file, args.env_file
                )
            elif action == "_run":
                return parallel.run_managed(
                    args.config, args.cloudflared, args.token_file, args.env_file
                )
            else:
                return _fail(f"unknown cloudflare-parallel command {action!r}")
            return 0
        except WindowsCloudflareParallelError as exc:
            return _fail(str(exc))
    if command == "cloudflare":
        if args.cloudflare_command in {"stage", "unstage"} and sys.platform != "darwin":
            return _fail("independent Cloudflare Connector staging currently requires macOS")
        if sys.platform == "win32":
            from . import windows_service
            from .windows_service import WindowsServiceError

            try:
                action = args.cloudflare_command
                if action == "install":
                    state = windows_service.install_cloudflare_service(
                        args.config,
                        args.cloudflared,
                        args.token_file,
                        args.env_file,
                        args.http_port,
                        args.hostname,
                    )
                    sys.stderr.write(json.dumps(state, sort_keys=True) + "\n")
                elif action == "status":
                    sys.stderr.write(
                        json.dumps(windows_service.cloudflare_status(), sort_keys=True) + "\n"
                    )
                elif action == "start":
                    sys.stderr.write(
                        json.dumps(windows_service.cloudflare_start(), sort_keys=True) + "\n"
                    )
                elif action == "stop":
                    sys.stderr.write(
                        json.dumps(windows_service.cloudflare_stop(), sort_keys=True) + "\n"
                    )
                elif action == "restart":
                    sys.stderr.write(
                        json.dumps(windows_service.cloudflare_restart(), sort_keys=True) + "\n"
                    )
                elif action == "uninstall":
                    windows_service.cloudflare_uninstall()
                    sys.stderr.write("Windows Cloudflare provider uninstalled\n")
                return 0
            except WindowsServiceError as exc:
                return _fail(str(exc))
    if sys.platform == "win32":
        if getattr(args, "component", None) is not None:
            return _fail("--component connector-only lifecycle is not available on Windows")
        from . import windows_service
        from .windows_service import WindowsServiceError

        try:
            if command == "_watchdog":
                return windows_service.run_watchdog(args.config, args.env_file)
            if command == "_run":
                return windows_service.run_managed(args.config, args.env_file)
            if command == "install":
                config_path = args.config.resolve()
                env_file = args.env_file
                if env_file is None:
                    candidate = config_path.parent / ".env"
                    env_file = candidate if candidate.is_file() else None
                if env_file is None:
                    return _fail("service install requires --env-file or a sibling .env")
                snapshot = windows_service.install(config_path, env_file.resolve())
                sys.stderr.write(json.dumps(snapshot, sort_keys=True) + "\n")
                return 0
            if command == "status":
                native_status = windows_service.status()
                status_format = getattr(args, "status_format", "native")
                if status_format == "native":
                    sys.stderr.write(json.dumps(native_status, sort_keys=True) + "\n")
                else:
                    from .service_observability import (
                        render_common_human,
                        render_common_json,
                        windows_common_projection,
                    )

                    snapshot, policy = windows_common_projection(native_status)
                    renderer = (
                        render_common_json
                        if status_format == "common-json"
                        else render_common_human
                    )
                    sys.stderr.write(renderer(snapshot, policy))
                return 0
            if command == "start":
                sys.stderr.write(json.dumps(windows_service.start(), sort_keys=True) + "\n")
                return 0
            if command == "restart":
                sys.stderr.write(json.dumps(windows_service.restart(), sort_keys=True) + "\n")
                return 0
            if command == "stop":
                sys.stderr.write(json.dumps(windows_service.stop(), sort_keys=True) + "\n")
                return 0
            if command == "uninstall":
                windows_service.uninstall()
                sys.stderr.write("persistent Windows service uninstalled\n")
                return 0
            if command == "open-settings":
                return _fail("open-settings is macOS-only")
        except WindowsServiceError as exc:
            return _fail(str(exc))
        return _fail(f"unknown service command {command!r}")
    return _fail(f"unknown service command {command!r}")


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
