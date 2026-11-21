"""Four separate Task Scheduler entrypoints, with no coupled Windows owner.

Core uses the existing cross-platform ServerFS MCP HTTP engine.
Bridge uses cross-platform Agent IPC/lease and Win32 named Job primitives.
Each ingress runs only its own transport process in an anonymous Job.
No component invokes an old W2 Core/Bridge/Connector lifecycle controller.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentityError, valid_id

from . import storage
from .spec import COMPONENTS, Manifest


class RuntimeErrorV25(Exception):
    """Fixed non-secret current-user runtime failure."""


_ENV = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
    }
)
_HTTP_KEYS = frozenset(
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


def _verified_owner(
    component: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    connector_id: str | None = None,
) -> Manifest:
    if sys.platform != "win32" or os.name != "nt":
        raise RuntimeErrorV25("native Windows component required")
    if component not in COMPONENTS or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeErrorV25("component process startup identity is invalid")
    try:
        valid_id(instance_id)
        if connector_id is not None:
            if component not in ("cloudflare", "openai") or instance_id == DEFAULT_INSTANCE_ID:
                raise RuntimeErrorV25("Connector process identity is invalid")
            valid_id(connector_id)
    except InstanceIdentityError as exc:
        raise RuntimeErrorV25("instance process startup identity is invalid") from exc
    manifest = (
        storage.load(component, instance_id, connector_id)
        if connector_id is not None
        else storage.load(component, instance_id)
        if instance_id != DEFAULT_INSTANCE_ID
        else storage.load(component)
    )
    expected = (
        storage.pinned_pythonw(component)
        if instance_id == DEFAULT_INSTANCE_ID
        else storage.pinned_pythonw(component, instance_id)
    )
    if os.path.normcase(str(Path(sys.executable).resolve(strict=True))) != os.path.normcase(
        str(expected.resolve(strict=True))
    ) or os.path.normcase(str(Path(sys.prefix).resolve(strict=True))) != os.path.normcase(
        str(expected.parent.parent.resolve(strict=True))
    ):
        raise RuntimeErrorV25("component does not run from its registered private venv")
    storage.check_private(expected, directory=False, sid=manifest.sid)
    return manifest


def _roots(config: Path) -> tuple[Path, list[Path]]:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    try:
        _no_reparse_components(config)
        source = config.resolve(strict=True)
        if not source.is_file() or source.suffix.casefold() != ".toml":
            raise ValueError("Core config must be a TOML file")
        workdirs, _settings = load_native_config(source)
        if not workdirs:
            raise ValueError("Core workdir policy is empty")
        roots = []
        for item in workdirs:
            _no_reparse_components(item.root)
            roots.append(item.root.resolve(strict=True))
        return source, roots
    except Exception as exc:  # noqa: BLE001 - no config/host paths in error
        raise RuntimeErrorV25("native workdir policy is invalid") from exc


def _private_input(path: Path, roots: list[Path], sid: str) -> Path:
    from serverfs_mcp.windows_cloudflare_security import validate_private_file

    checkout = Path(__file__).resolve().parents[3]
    try:
        return validate_private_file(
            path, checkout_root=checkout, workdir_roots=roots, user_sid=sid
        )
    except Exception as exc:  # noqa: BLE001 - no secret paths
        raise RuntimeErrorV25("component private input cannot be verified") from exc


def _core_configuration(manifest: Manifest) -> tuple[Path, dict[str, str]]:
    from serverfs_mcp.config import settings_from_env
    from serverfs_mcp.windows_service import _read_env_file

    options = manifest.settings
    config, roots = _roots(Path(str(options["config"])))
    auth_file = _private_input(Path(str(options["auth_file"])), roots, manifest.sid)
    try:
        values = _read_env_file(auth_file)
        if set(values) - _HTTP_KEYS:
            raise ValueError("unsupported HTTP authentication fields")
        hostname = options["hostname"]
        port = options["port"]
        hosts = values.get("SERVERFS_HTTP_ALLOWED_HOSTS", "").split(",")
        if (
            type(hostname) is not str
            or type(port) is not int
            or len(hosts) != 2
            or set(hosts) != {hostname, f"127.0.0.1:{port}"}
        ):
            raise ValueError("Core HTTP Host policy differs from ingress identity")
        parsed = settings_from_env(values)
        if bool(parsed.http_api_key) == bool(parsed.oauth_enabled):
            raise ValueError("exactly one HTTP authorization mode is required")
        if parsed.oauth_enabled and not parsed.oauth_introspection_secret:
            raise ValueError("OAuth introspection secret is absent")
        return config, values
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeErrorV25("Core HTTP policy cannot be validated") from exc


def run_core(manifest: Manifest) -> int:
    """One resident native MCP HTTP Core; zero child owners or tunnels."""
    from .readiness import publish

    config, auth = _core_configuration(manifest)
    agent_wiring = None
    if manifest.instance_id != DEFAULT_INSTANCE_ID:
        from serverfs_mcp.native_config import load_native_config

        workdirs, policy = load_native_config(config)
        if policy.bridge_required:
            from .core_bridge_binding import load_native_core_bridge_binding

            binding = load_native_core_bridge_binding(manifest, workdirs)
            agent_wiring = binding.build_wiring(log_level=policy.log_level)
        from .isolation import assert_core_isolation

        assert_core_isolation(manifest, storage.core_inventory())
    # Preserve only Windows identity/runtime keys, plus EXPLICIT validated
    # HTTP credentials; no ambient provider, proxy or Tunnel variables.
    base = {k: v for k, v in os.environ.items() if k.upper() in _ENV}
    base["LOCALAPPDATA"] = str(storage.root().parent.parent)
    if manifest.instance_id != DEFAULT_INSTANCE_ID:
        base["SERVERFS_DATA_HOME"] = str(storage.root(manifest.instance_id))
    publish(manifest)
    os.environ.clear()
    os.environ.update(base | auth)
    from serverfs_mcp import cli

    arguments = [
        "serve",
        "--config",
        str(config),
        "--transport",
        "streamable-http",
        "--http-port",
        str(manifest.settings["port"]),
    ]
    if agent_wiring is None:
        # Preserve the legacy native Core invocation, including no injected
        # bridge client, for default and nondefault file-only instances.
        return cli.main([*arguments])
    return cli.main(arguments, agent_wiring_override=agent_wiring)


def run_bridge(manifest: Manifest) -> int:
    """One Bridge-owned Job/lease; default preserves the existing v1 generation."""
    if manifest.instance_id != DEFAULT_INSTANCE_ID:
        return run_independent_bridge_v2(manifest)
    from serverfs_mcp.agent_lifecycle import (
        agent_proxy_from_environment,
        await_graceful_exit,
        bootstrap_frame_bytes,
        bridge_child_environment,
        render_request_from_config,
        request_graceful_shutdown,
        spawn_bridge,
        wait_for_bridge_readiness,
    )
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_lifecycle import LifecycleLease, job_name
    from serverfs_mcp.supervisor import _render_bridge_config
    from serverfs_mcp.windows_job import WindowsJob

    from .readiness import publish

    config, _roots_value = _roots(Path(str(manifest.settings["config"])))
    bridge_python = Path(str(manifest.settings["bridge_python"]))
    storage.check_private(bridge_python, directory=False, sid=manifest.sid)
    if bridge_python.name.casefold() != "python.exe":
        raise RuntimeErrorV25("private Bridge runtime interpreter differs")
    # Acquiring the existing SID lease must fail if the legacy Tunnel-owned
    # Bridge still exists. Never make a second same-SID Bridge endpoint.
    lease = LifecycleLease().acquire()
    bridge = None
    try:
        workdirs, settings = load_native_config(config)
        request = render_request_from_config(workdirs, settings)
        rendered = _render_bridge_config(request, bridge_python=bridge_python)
        proxy = agent_proxy_from_environment(settings.agent, os.environ)
        child_env = bridge_child_environment(proxy)
        child_env.pop("SERVERFS_BRIDGE_PYTHON", None)
        with WindowsJob(job_name()) as job:
            try:
                bridge = spawn_bridge(
                    config_path=rendered.config_path,
                    child_env=child_env,
                    job=job,
                    argv=[
                        str(bridge_python),
                        "-I",
                        "-B",
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(rendered.config_path),
                        "--supervised",
                    ],
                )
                if bridge.stdin is None:
                    raise RuntimeErrorV25("Bridge lifecycle bootstrap pipe is absent")
                frame = bootstrap_frame_bytes(proxy, prior_bridge_execution_stopped=True)
                if bridge.stdin.write(frame) != len(frame):
                    raise RuntimeErrorV25("Bridge bootstrap frame is incomplete")
                bridge.stdin.flush()
                asyncio.run(
                    wait_for_bridge_readiness(
                        rendered.socket_path, bridge, server_pid_validator=job.contains_pid
                    )
                )
                publish(manifest)
                while bridge.poll() is None:
                    time.sleep(0.4)
                return 3  # Bridge exited: OS Task Scheduler restarts only this task.
            finally:
                if bridge is not None:
                    request_graceful_shutdown(bridge)
                    await_graceful_exit(bridge)
            # Job close kills only the Bridge/providers, never the Core/ingress.
    finally:
        lease.close()


def run_independent_bridge_v2(manifest: Manifest) -> int:
    """Own a distinct v2-only Bridge without adopting or stopping legacy v1.

    Each owner has a protected instance root, its own kernel Job, a dedicated
    named pipe and a SID+bridge_id lease. Only an HMAC-authenticated health
    response from that exact Job generation completes startup.
    """
    from serverfs_mcp.agent_lifecycle import (
        await_graceful_exit,
        bootstrap_frame_bytes,
        bridge_child_environment,
        request_graceful_shutdown,
        spawn_bridge,
    )
    from serverfs_mcp.native_lifecycle import LifecycleLease
    from serverfs_mcp.windows_job import WindowsJob

    from .bridge_v2_owner import load_native_bridge_plan, wait_for_signed_bridge_v2_ready
    from .cli import _assert_instance_isolation
    from .legacy_bridge_ownership import assert_legacy_writable_roots_disjoint
    from .readiness import publish

    plan = load_native_bridge_plan(manifest)
    _assert_instance_isolation(manifest)
    assert_legacy_writable_roots_disjoint(manifest)
    lease = LifecycleLease(
        root=storage.home("bridge", manifest.instance_id),
        artifact_name=plan.lease_artifact,
    ).acquire()
    bridge = None
    try:
        with WindowsJob(plan.job_name) as job:
            try:
                child_env = bridge_child_environment(None)
                child_env.pop("SERVERFS_BRIDGE_PYTHON", None)
                bridge = spawn_bridge(
                    config_path=plan.config_path,
                    child_env=child_env,
                    job=job,
                    argv=[
                        str(plan.bridge_python),
                        "-I",
                        "-B",
                        "-m",
                        "serverfs_agent_bridge.main",
                        "--config",
                        str(plan.config_path),
                        "--supervised",
                    ],
                )
                if bridge.stdin is None:
                    raise RuntimeErrorV25("independent Bridge bootstrap is unavailable")
                frame = bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True)
                if bridge.stdin.write(frame) != len(frame):
                    raise RuntimeErrorV25("independent Bridge bootstrap is incomplete")
                bridge.stdin.flush()
                asyncio.run(
                    wait_for_signed_bridge_v2_ready(
                        plan, bridge, server_pid_validator=job.contains_pid
                    )
                )
                publish(manifest)
                while bridge.poll() is None:
                    time.sleep(0.4)
                return 3
            finally:
                if bridge is not None:
                    request_graceful_shutdown(bridge)
                    await_graceful_exit(bridge)
    finally:
        lease.close()


def _core_is_verified(manifest: Manifest) -> bool:
    from .readiness import core_ready
    from .tasks import Scheduler

    core = (
        storage.load("core")
        if manifest.instance_id == DEFAULT_INSTANCE_ID
        else storage.load("core", manifest.instance_id)
    )
    if (
        manifest.settings["port"] != core.settings["port"]
        or manifest.settings["hostname"] != core.settings["hostname"]
    ):
        return False
    state = Scheduler().status(core)
    return state["running"] is True and core_ready(core)


def _minimal_env(*, openai: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV}
    env["LOCALAPPDATA"] = str(storage.root().parent.parent)
    if openai:
        env["HEALTH_LISTEN_ADDR"] = "127.0.0.1:0"
    return env


def _connector_command(manifest: Manifest, roots: list[Path]) -> tuple[Path, list[str]]:
    info = manifest.settings
    if manifest.component == "cloudflare":
        binary = _private_input(Path(str(info["cloudflared"])), roots, manifest.sid)
        token = _private_input(Path(str(info["token_file"])), roots, manifest.sid)
        return binary, [
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token-file",
            str(token),
        ]
    if manifest.component != "openai":
        raise RuntimeErrorV25("invalid ingress component")
    binary = _private_input(Path(str(info["tunnel_client"])), roots, manifest.sid)
    control = _private_input(Path(str(info["control_plane_key_file"])), roots, manifest.sid)
    header = _private_input(Path(str(info["core_auth_header_file"])), roots, manifest.sid)
    url = f"http://127.0.0.1:{info['port']}/mcp"
    return binary, [
        "run",
        "--control-plane.tunnel-id",
        str(info["tunnel_id"]),
        "--control-plane.api-key",
        "file:" + str(control),
        "--mcp.server-url",
        "url=" + url,
        "--mcp.extra-headers",
        "Authorization: file:" + str(header),
        "--mcp.discovery-extra-headers",
        "Authorization: file:" + str(header),
    ]


def run_connector(manifest: Manifest) -> int:
    """One external ingress binary, bounded inside its OWN anonymous Job."""
    from serverfs_mcp.windows_job import WindowsJob

    from .readiness import publish

    if manifest.instance_id != DEFAULT_INSTANCE_ID:
        from .isolation import assert_connector_isolation

        peers = (
            *storage.component_inventory("cloudflare"),
            *storage.component_inventory("openai"),
        )
        assert_connector_isolation(manifest, peers)

    core = (
        storage.load("core")
        if manifest.instance_id == DEFAULT_INSTANCE_ID
        else storage.load("core", manifest.instance_id)
    )
    config, roots = _roots(Path(str(core.settings["config"])))
    if not config.is_file():
        raise RuntimeErrorV25("Core native config is not available")
    if not _core_is_verified(manifest):
        raise RuntimeErrorV25("independent authenticated Core is not ready")
    binary, args = _connector_command(manifest, roots)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004
    process = None
    with WindowsJob() as job:
        try:
            process = subprocess.Popen(
                [str(binary), *args],
                cwd=binary.parent,
                env=_minimal_env(openai=manifest.component == "openai"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            job.assign(process)
            job.resume(process)
            publish(manifest)
            while process.poll() is None:
                time.sleep(5.0)
                if not _core_is_verified(manifest):
                    raise RuntimeErrorV25("Core changed; connector generation must stop")
            return process.wait() or 3
        except Exception:
            # Assignment can fail while a child is still CREATE_SUSPENDED
            # and not yet in our anonymous Job. Reap exactly that one
            # newly created process before leaving the kill-on-close scope.
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            raise


def _attach_windowless_stdio(manifest: Manifest) -> None:
    """Give pythonw.exe a private error/log stream before importing HTTP.

    CPython's windowless Windows interpreter has no attached console, so
    sys.stdout/sys.stderr can be None. Structured logging writes to stderr;
    leaving None crashes the HTTP Core *after* identity publication but
    *before* uvicorn binds. The same issue affects any other v25 component.
    """
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    root = (
        storage.home(manifest.component, manifest.instance_id, manifest.connector_id)
        if manifest.connector_id is not None
        else storage.home(manifest.component, manifest.instance_id)
        if manifest.instance_id != DEFAULT_INSTANCE_ID
        else storage.home(manifest.component)
    )
    storage.check_private(root, directory=True, sid=manifest.sid)
    log = root / "runtime.log"
    if log.exists() or log.is_symlink():
        storage.check_private(log, directory=False, sid=manifest.sid)
    else:
        with log.open("xb"):
            pass
        try:
            protect_new_file(log, manifest.sid)
            storage.check_private(log, directory=False, sid=manifest.sid)
        except Exception:
            log.unlink(missing_ok=True)
            raise
    # Keep a bounded private log across Task Scheduler restarts. Never log
    # credentials or manifest contents here; the server owns its own logging.
    mode = "w" if log.stat().st_size > 8 * 1024 * 1024 else "a"
    stream = log.open(mode, encoding="utf-8", buffering=1)
    sys.stderr = stream
    sys.stdout = stream


def main(argv: list[str] | None = None) -> int:
    # pythonw.exe does not have a console. A writable stderr is mandatory
    # even for the early malformed-argument and owner-refusal branches.
    windowless = sys.stderr is None or sys.stdout is None
    if windowless:
        sink = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sink
        sys.stdout = sink
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2, 3) or args[0] not in COMPONENTS:
        print("windows-v25: fixed component name required", file=sys.stderr)
        return 2
    component = args[0]
    instance_id = args[1] if len(args) >= 2 else DEFAULT_INSTANCE_ID
    connector_id = args[2] if len(args) == 3 else None
    try:
        manifest = (
            _verified_owner(component, instance_id, connector_id)
            if connector_id is not None
            else _verified_owner(component, instance_id)
            if instance_id != DEFAULT_INSTANCE_ID
            else _verified_owner(component)
        )
        if windowless:
            _attach_windowless_stdio(manifest)
        if component == "core":
            return run_core(manifest)
        if component == "bridge":
            return run_bridge(manifest)
        return run_connector(manifest)
    except Exception as exc:  # noqa: BLE001 - no raw paths or provider output
        print(f"windows-v25: {component} failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
