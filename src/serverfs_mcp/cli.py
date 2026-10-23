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
import atexit
import json
import os
import sys
import time
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
    service_sub.add_parser("start", help="Register configured persistent services")
    service_restart = service_sub.add_parser(
        "restart", help="Unregister then register configured services"
    )
    service_restart.add_argument("--_handover-child", action="store_true", help=argparse.SUPPRESS)
    service_sub.add_parser("stop", help="Unregister configured persistent services")
    service_sub.add_parser(
        "uninstall",
        help="Unregister services and remove platform-owned persistent lifecycle artifacts",
    )
    service_sub.add_parser("open-settings", help="Open System Settings > Login Items (macOS)")
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


def _register_handover_finalizer(darwin_service) -> None:
    """Schedule verified cleanup at process exit for every handover-child return path."""

    def finalize() -> None:
        try:
            darwin_service.schedule_handover_cleanup()
        except darwin_service.DarwinServiceError:
            pass  # schedule_handover_cleanup persists final failure evidence.

    atexit.register(finalize)


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


def _darwin_ingress_helper(settings):  # noqa: ANN001 - Settings (avoid import cycle at module scope)
    """Spawn the native file-ingress helper for a darwin serve (Phase H).

    ServerFS MCP itself never fetches ChatGPT temporary URLs; the helper is
    a separate process the supervisor owns, reached over a private AF_UNIX
    HTTP socket in the per-user runtime dir. The child carries a parent
    guard so a killed supervisor cannot orphan a fetch-capable helper.
    """
    import subprocess as _subprocess
    import time as _time

    from .config import Settings
    from .darwin_libc import darwin_runtime_dir
    from .file_ingress_client import FileIngressClient

    assert isinstance(settings, Settings)
    runtime = darwin_runtime_dir() / "file-ingress-v1"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = runtime / "ingress.sock"
    child_env = {
        **os.environ,
        "SERVERFS_FILE_INGRESS_ENABLED": "true",
        "SERVERFS_FILE_INGRESS_SOCKET": str(socket_path),
        "SERVERFS_FILE_INGRESS_PARENT_GUARD": "1",
    }
    process = _subprocess.Popen(
        [sys.executable, "-m", "serverfs_mcp.file_ingress"],
        env=child_env,
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
    )
    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        if socket_path.exists():
            return FileIngressClient(
                timeout_seconds=settings.file_ingress_timeout_seconds,
                socket_path=socket_path,
            )
        if process.poll() is not None:
            raise SystemExit(_fail("file ingress helper exited during startup"))
        _time.sleep(0.1)
    process.kill()
    raise SystemExit(_fail("file ingress helper did not become ready in time"))


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
    if sys.platform == "darwin" and native_settings.agent_enabled:
        from .darwin_bridge_policy import DarwinBridgePolicyError, policy_is_synced

        try:
            synced = policy_is_synced(workdirs, native_settings)
        except DarwinBridgePolicyError as exc:
            return _fail(f"Agent Bridge policy check failed: {exc}")
        if not synced:
            return _fail(
                "Agent Bridge policy is stale relative to serverfs.toml; run "
                "'serverfs agent-bridge restart --config <serverfs.toml>' before starting serve"
            )
    from .config import Settings
    from .main import create_server
    from .workdirs import WorkdirRegistry

    agent_wiring = _native_agent_settings(native_settings)
    if agent_wiring is None:
        settings = Settings(log_level=native_settings.log_level)
        client = None
    else:
        settings, client = agent_wiring
    settings = _overlay_ingress_env(settings)
    file_ingress_client = None
    if settings.file_ingress_enabled and sys.platform == "darwin":
        helper = _darwin_ingress_helper(settings)
        if helper is not None:
            file_ingress_client = helper
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
    import platform as _platform

    if sys.platform != "darwin":
        return _fail("agent-bridge lifecycle management is macOS-only in v0.13")
    if _platform.machine() != "arm64":
        return _fail("the macOS Agent Bridge runs on Apple Silicon only")

    from . import darwin_service
    from .darwin_service import DarwinServiceError

    command = args.agent_command
    if command == "install":
        return _fail(
            "v0.13.2 no longer installs a standalone ~/Library/LaunchAgents plist; use "
            "'serverfs service install --config <serverfs.toml> --env-file <.env>'"
        )
    if command == "configure":
        from .darwin_bridge_policy import DarwinBridgePolicyError, configure_policy

        workdirs, native_settings = _load(args.config)
        if not native_settings.agent_enabled:
            return _fail("the native configuration does not enable Agent delegation")
        try:
            configure_policy(workdirs, native_settings, env_file=args.env_file)
        except DarwinBridgePolicyError as exc:
            return _fail(f"Agent Bridge configuration failed: {exc}")
        sys.stderr.write("Agent Bridge private configuration published\n")
        return 0
    if command == "sync":
        from .darwin_bridge_policy import DarwinBridgePolicyError, sync_policy

        workdirs, native_settings = _load(args.config)
        if not native_settings.agent_enabled:
            return _fail("the native configuration does not enable Agent delegation")
        try:
            sync_policy(workdirs, native_settings)
        except DarwinBridgePolicyError as exc:
            return _fail(f"Agent Bridge policy sync failed: {exc}")
        sys.stderr.write("Agent Bridge policy synchronized\n")
        return 0
    try:
        if command == "start":
            darwin_service.register_service(darwin_service.BRIDGE_SERVICE)
            return 0
        if command == "restart":
            if args.env_file is not None and args.config is None:
                return _fail("--env-file requires --config for Agent Bridge restart")
            if args.config is not None:
                from .darwin_bridge_policy import (
                    DarwinBridgePolicyError,
                    configure_policy,
                    sync_policy,
                )

                workdirs, native_settings = _load(args.config)
                if not native_settings.agent_enabled:
                    return _fail("the native configuration does not enable Agent delegation")
                try:
                    if args.env_file is None:
                        sync_policy(workdirs, native_settings)
                    else:
                        configure_policy(workdirs, native_settings, env_file=args.env_file)
                except DarwinBridgePolicyError as exc:
                    return _fail(f"Agent Bridge policy sync failed: {exc}")
            if bool(getattr(args, "_handover_child", False)):
                _register_handover_finalizer(darwin_service)
                try:
                    darwin_service.restart_service(darwin_service.BRIDGE_SERVICE)
                except DarwinServiceError as exc:
                    darwin_service.write_handover_result(ok=False, detail=str(exc), complete=False)
                    raise
                darwin_service.write_handover_result(
                    ok=True, detail="agent-bridge restarted", complete=False
                )
            else:
                darwin_service.schedule_handover("bridge-restart")
                sys.stderr.write(
                    "controlled macOS Agent Bridge restart hand-over scheduled via launchd\n"
                )
            return 0
        if command == "stop":
            darwin_service.unregister_service(darwin_service.BRIDGE_SERVICE)
            return 0
        if command == "status":
            service_status = darwin_service.status()
            states = service_status.get("services", {})
            processes = service_status.get("processes", {})
            sys.stderr.write(f"state: {states.get(darwin_service.BRIDGE_SERVICE, 'unknown')}\n")
            if isinstance(processes, dict):
                bridge = processes.get(darwin_service.BRIDGE_SERVICE, {})
                if isinstance(bridge, dict):
                    sys.stderr.write(f"pid: {bridge.get('pid') or ''}\n")
            return 0
        if command == "uninstall":
            darwin_service.unregister_service(darwin_service.BRIDGE_SERVICE)
            sys.stderr.write("Agent Bridge service unregistered\n")
            return 0
    except DarwinServiceError as exc:
        return _fail(str(exc))
    return _fail(f"unknown agent-bridge command {command!r}")


def cmd_service(args: argparse.Namespace) -> int:
    command = args.service_command
    if sys.platform == "win32":
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

    if sys.platform != "darwin":
        return _fail("persistent service management is supported on macOS and Windows only")

    from . import darwin_service
    from .darwin_service import DarwinServiceError

    try:
        if command == "install":
            config_path = args.config.resolve()
            env_file = args.env_file
            if env_file is None:
                candidate = config_path.parent / ".env"
                env_file = candidate if candidate.is_file() else None
            if env_file is None:
                return _fail("service install requires --env-file or a sibling .env")
            env_file = env_file.resolve()
            handover_child = bool(getattr(args, "_handover_child", False))
            if handover_child:
                _register_handover_finalizer(darwin_service)
            expected_roots: list[int] | None = None
            if handover_child:
                raw_roots = (getattr(args, "_handover_roots", None) or "").strip()
                try:
                    expected_roots = sorted(
                        {int(value) for value in raw_roots.split(",") if value.strip()}
                    )
                except ValueError:
                    darwin_service.write_handover_result(
                        ok=False, detail="invalid frozen Tunnel root PID list", complete=False
                    )
                    return _fail("invalid frozen Tunnel root PID list")
                if any(pid <= 0 for pid in expected_roots):
                    darwin_service.write_handover_result(
                        ok=False, detail="invalid frozen Tunnel root PID list", complete=False
                    )
                    return _fail("invalid frozen Tunnel root PID list")
            else:
                unmanaged_roots = darwin_service.find_unmanaged_tunnel_roots()
                legacy_bridge = darwin_service.legacy_bridge_plist_path().is_file()
                if unmanaged_roots or legacy_bridge:
                    darwin_service.schedule_handover_install(config_path, env_file, unmanaged_roots)
                    sys.stderr.write(
                        "controlled v0.13.2 lifecycle hand-over scheduled via launchd\n"
                    )
                    return 0

            workdirs, native_settings = _load(config_path)
            if native_settings.agent_enabled:
                from .darwin_bridge_policy import DarwinBridgePolicyError, configure_policy

                try:
                    configure_policy(workdirs, native_settings, env_file=env_file)
                except DarwinBridgePolicyError as exc:
                    if handover_child:
                        darwin_service.write_handover_result(
                            ok=False, detail=str(exc), complete=False
                        )
                    return _fail(f"Agent Bridge configuration failed: {exc}")
            states = darwin_service.install(
                config_path,
                env_file,
                bridge_enabled=native_settings.agent_enabled,
                expected_unmanaged_roots=expected_roots,
            )
            for service, state in sorted(states.items()):
                sys.stderr.write(f"{service}: {state}\n")
            if handover_child:
                darwin_service.write_handover_result(ok=True, detail="installed", complete=False)
            return 0
        if command == "status":
            snapshot = darwin_service.status()
            status_format = getattr(args, "status_format", "native")
            if status_format == "native":
                sys.stderr.write(json.dumps(snapshot, sort_keys=True) + "\n")
            else:
                from .service_observability import (
                    darwin_common_projection,
                    render_common_human,
                    render_common_json,
                )

                observation, policy = darwin_common_projection(snapshot)
                renderer = (
                    render_common_json if status_format == "common-json" else render_common_human
                )
                sys.stderr.write(renderer(observation, policy))
            return 0
        if command == "start":
            states = darwin_service.start()
            for service, state in sorted(states.items()):
                sys.stderr.write(f"{service}: {state}\n")
            return 0
        if command == "restart":
            handover_child = bool(getattr(args, "_handover_child", False))
            if handover_child:
                _register_handover_finalizer(darwin_service)
            if not handover_child:
                manifest = darwin_service._load_manifest_document()
                if darwin_service.BRIDGE_SERVICE in manifest["services"]:
                    darwin_service.schedule_handover("restart")
                    sys.stderr.write("controlled macOS restart hand-over scheduled via launchd\n")
                    return 0
            try:
                states = darwin_service.restart()
            except DarwinServiceError as exc:
                if handover_child:
                    darwin_service.write_handover_result(ok=False, detail=str(exc), complete=False)
                raise
            for service, state in sorted(states.items()):
                sys.stderr.write(f"{service}: {state}\n")
            if handover_child:
                darwin_service.write_handover_result(ok=True, detail="restarted", complete=False)
            return 0
        if command == "_handover-cleanup":
            time.sleep(1)
            darwin_service.cleanup_handover_job()
            return 0
        if command == "stop":
            states = darwin_service.stop()
            for service, state in sorted(states.items()):
                sys.stderr.write(f"{service}: {state}\n")
            return 0
        if command == "uninstall":
            darwin_service.uninstall()
            sys.stderr.write("persistent services uninstalled\n")
            return 0
        if command == "open-settings":
            darwin_service.open_settings()
            return 0
    except DarwinServiceError as exc:
        if command == "install" and bool(getattr(args, "_handover_child", False)):
            try:
                darwin_service.write_handover_result(ok=False, detail=str(exc), complete=False)
            except DarwinServiceError:
                pass
        if command == "restart" and bool(getattr(args, "_handover_child", False)):
            try:
                darwin_service.write_handover_result(ok=False, detail=str(exc), complete=False)
            except DarwinServiceError:
                pass
        return _fail(str(exc))
    return _fail(f"unknown service command {command!r}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
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
