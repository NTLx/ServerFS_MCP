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
    agent_sub.add_parser("start", help="Kickstart the LaunchAgent")
    agent_sub.add_parser("restart", help="Kickstart -k the LaunchAgent")
    agent_sub.add_parser("stop", help="Boot the LaunchAgent out")
    agent_sub.add_parser("status", help="Read-only launchctl print summary")
    agent_sub.add_parser("uninstall", help="Boot the LaunchAgent out and remove the plist")
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


def cmd_agent_bridge(args: argparse.Namespace) -> int:
    import platform as _platform

    if sys.platform != "darwin":
        return _fail("agent-bridge lifecycle management is macOS-only in v0.13")
    if _platform.machine() != "arm64":
        return _fail("the macOS Agent Bridge runs on Apple Silicon only")
    from . import darwin_lifecycle as lifecycle
    from .native_endpoint import bridge_home

    command = args.agent_command
    try:
        if command == "install":
            plan = lifecycle.LaunchAgentPlan(
                bridge_executable=(
                    args.bridge_executable
                    if args.bridge_executable is not None
                    else lifecycle.default_bridge_executable()
                ),
                bridge_config_path=args.bridge_config,
                log_dir=args.log_dir if args.log_dir is not None else bridge_home() / "logs",
            )
            path = lifecycle.install(plan, force=args.force)
            sys.stderr.write(f"LaunchAgent installed and bootstrapped: {path}\n")
            return 0
        if command == "start":
            lifecycle.start()
            return 0
        if command == "restart":
            lifecycle.restart()
            return 0
        if command == "stop":
            lifecycle.stop()
            return 0
        if command == "status":
            for key, value in lifecycle.status().items():
                sys.stderr.write(f"{key}: {value}\n")
            return 0
        if command == "uninstall":
            lifecycle.uninstall()
            sys.stderr.write("LaunchAgent removed\n")
            return 0
    except lifecycle.LaunchAgentError as exc:
        return _fail(str(exc))
    return _fail(f"unknown agent-bridge command {command!r}")


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
    if args.command == "agent-bridge":
        return cmd_agent_bridge(args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
