"""Independent Agent Bridge owner (v0.25.0 W1a) — source only, not installed or registered.

**Why this module exists.** The W0 operator report measured the current Windows chain:
`tunnel-client → serverfs_mcp.supervisor.run_with_agent → LifecycleLease + named WindowsJob →
Bridge → provider descendants`. The Bridge is a depth-4 descendant of `tunnel-client`, so ending the
tunnel tears the Bridge and every provider down with it. v0.25.0 requires the Bridge to have its own
process and restart ownership, which means something other than the tunnel must be able to start it.
This module is that owner, at source level only.

**What it owns.** Exactly the same two kernel objects the supervisor claims, in the same order:
the per-user `LifecycleLease`, then a *fresh* named Windows Job, and — inside that Job — one Bridge
process holding its own provider descendants. It owns nothing else. In particular it does **not**
start the ServerFS stdio MCP child, an HTTP Core, or a Connector: that is the whole point of the
split, and a future supervising service must be able to end this owner without ending them.

**Why the ordering is load-bearing.**

- The lease is acquired **before** any configuration is rendered or any process exists. It is the
  only exclusive statement that no earlier owner of this user is alive, so acquiring it after
  spawning would make it worthless.
- `job.open()` **before** the bootstrap frame. A named job object is destroyed once its last handle
  closes, and closing it is what delivers kill-on-close to every member; a *free* name therefore
  means the object that held the previous provider tree is gone and Windows has ordered that tree to
  stop. A name that is already taken raises, and no frame is sent — a claim that cannot be
  established is never made.
- `job.close()` last in the shutdown path, and the lease released after it. Releasing the lease
  first would let the next owner see a free lease over a generation that has not yet been told to
  stop.

**What this owner must never do**, because those are the failure modes v0.25.0 exists to remove:

- kill or signal a Core or an ingress when the Bridge fails. A Bridge failure is reported to the
  caller as an exit code; nothing else in the chain is touched.
- treat `active_task_count == 0` (or any Bridge-reported count) as proof that providers are gone, or
  clear a workdir recovery guard. Only the Job close proves containment.
- reach around the lease or the job namespace by inventing another identity, an anonymous job or a
  second Bridge. A taken lease or a taken job name is a refusal, not an obstacle.

**Status.** W1a delivers this source path and its isolated tests. Registering a Task Scheduler
manifest, component-scoped `service` commands, the independent Core and the connector-only
generation are later tasks; `main()` exists so a later owner has one narrow entrypoint, and is
deliberately not wired into `cli.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .agent_lifecycle import (
    AgentLifecycleError,
    agent_proxy_from_environment,
    await_graceful_exit,
    bootstrap_frame_bytes,
    bridge_child_environment,
    render_request_from_config,
    request_graceful_shutdown,
    spawn_bridge,
    wait_for_bridge_readiness,
)
from .native_config import load_native_config
from .native_lifecycle import LifecycleLease, LifecycleOwnershipError, job_name
from .windows_job import JobObjectError, WindowsJob

#: The owner stopped because it was asked to. The Bridge was shut down in order.
EXIT_OK = 0

#: The owner refused or failed before it could serve: lease busy, policy invalid, containment not
#: obtainable, Bridge unstartable or never ready. Nothing is left running.
EXIT_REFUSED = 2

#: The owner was serving and the Bridge exited underneath it. W1a does not restart or replace it;
#: deciding what a Bridge loss means belongs to the supervising service in a later task.
EXIT_BRIDGE_LOST = 3

#: How often the idle loop checks the Bridge and the stop request. The owner has no other work, so
#: this is a liveness check rather than a supervision policy.
IDLE_POLL_SECONDS = 0.5

# Longest line kept in a diagnostic. Messages are built from a failure class, never a payload.
_DIAGNOSTIC_LIMIT = 400


def _diagnostic(message: object) -> None:
    """Write one redacted line to stderr. Never a traceback, never an unvouched-for path."""
    # OS and native config exceptions can include absolute paths or user-provided values.
    # Only the three product-owned, redacted error classes are safe to print verbatim.
    if isinstance(message, (AgentLifecycleError, JobObjectError, LifecycleOwnershipError)):
        safe = str(message)
    elif isinstance(message, BaseException):
        safe = f"operation failed ({type(message).__name__})"
    else:
        safe = str(message)
    text = safe.replace("\n", " ")[:_DIAGNOSTIC_LIMIT]
    sys.stderr.write(f"serverfs-bridge-owner: {text}\n")
    sys.stderr.flush()


def _render_request(workdirs: object, settings: object) -> dict:
    """Render the Bridge request document from the parsed native config."""
    return render_request_from_config(workdirs, settings)


def _render_config(request: dict) -> object:
    """Render the private Bridge configuration through the Bridge-owned entry point.

    Imported from ``supervisor`` rather than reimplemented: the renderer invocation and its scrubbed
    environment are one frozen contract (``§23``/``§70``), and a second copy would be free to drift
    from the legacy path this owner has to stay compatible with.
    """
    from .supervisor import _render_bridge_config

    return _render_bridge_config(request)


def _render_config_pinned(request: dict, bridge_python: Path) -> object:
    """Render with the same Bridge-owned renderer, but a fixed private interpreter."""
    from .supervisor import _render_bridge_config

    return _render_bridge_config(request, bridge_python=bridge_python)


def _bridge_argv_for(config_path: Path) -> list[str]:
    """The Bridge command line, from the same frozen helper the legacy path uses."""
    from .supervisor import _bridge_argv

    return _bridge_argv(config_path)


def _pinned_bridge_argv(config_path: Path, bridge_python: Path) -> list[str]:
    """Use the separately validated private Bridge interpreter, never ambient env."""
    if not bridge_python.is_absolute() or bridge_python.name.casefold() != "python.exe":
        raise AgentLifecycleError("Bridge runtime interpreter is invalid")
    return [
        str(bridge_python),
        "-I",
        "-B",
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(config_path),
        "--supervised",
    ]


def _await_ready(socket_path: Path, process: subprocess.Popen[bytes]) -> None:
    """Authenticated readiness over the real Named Pipe, never a sleep or a bare pipe check."""
    asyncio.run(wait_for_bridge_readiness(socket_path, process))


def _await_ready_bound(
    socket_path: Path,
    process: subprocess.Popen[bytes],
    job: WindowsJob,
) -> None:
    """Require authenticated pipe server membership in this owner's held Job.

    PID equality is invalid for venv interpreter launchers: the real Python
    process may be a child. The Windows kernel proves the exact Job generation.
    """
    asyncio.run(
        wait_for_bridge_readiness(socket_path, process, server_pid_validator=job.contains_pid)
    )


def _default_lease() -> LifecycleLease:
    return LifecycleLease().acquire()


def _default_job() -> WindowsJob:
    return WindowsJob(job_name())


@dataclass(frozen=True)
class OwnerDependencies:
    """Every seam the owner touches, so one test can replace one behaviour without patching globals.

    The defaults are the production implementations. A test that needs an isolated lifecycle passes
    its own ``acquire_lease``/``make_job``; a test that must not start a process replaces ``spawn``.

    The ownership seams are resolved through ``default_factory`` rather than bound at class
    definition, so a test can substitute this module's ``_default_lease``/``_default_job`` *before*
    constructing the dependency set. That matters for safety rather than style: a test that forgot
    to inject an isolated lease must fail loudly instead of taking the real per-user lease and
    starting a real Bridge.
    """

    acquire_lease: Callable[[], LifecycleLease] = field(default_factory=lambda: _default_lease)
    make_job: Callable[[], WindowsJob] = field(default_factory=lambda: _default_job)
    load_config: Callable[[Path], object] = load_native_config
    render_request: Callable[[object, object], dict] = _render_request
    render_config: Callable[[dict], object] = _render_config
    parse_proxy: Callable[..., object] = agent_proxy_from_environment
    build_env: Callable[..., dict[str, str]] = bridge_child_environment
    bridge_argv: Callable[[Path], list[str]] = _bridge_argv_for
    spawn: Callable[..., subprocess.Popen[bytes]] = spawn_bridge
    await_ready: Callable[[Path, subprocess.Popen[bytes]], None] = _await_ready
    await_ready_bound: Callable[[Path, subprocess.Popen[bytes], WindowsJob], None] = (
        _await_ready_bound
    )
    request_stop: Callable[[subprocess.Popen[bytes]], None] = request_graceful_shutdown
    await_exit: Callable[[subprocess.Popen[bytes]], bool] = await_graceful_exit


def run(
    config_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
    poll_interval: float = IDLE_POLL_SECONDS,
    deps: OwnerDependencies | None = None,
    bridge_python: Path | None = None,
) -> int:
    """Own one Bridge generation until asked to stop, then shut it down in order.

    Returns ``EXIT_OK``, ``EXIT_REFUSED`` or ``EXIT_BRIDGE_LOST``. No exception escapes: an owner
    that is the service entrypoint must report a refusal as a code, not as a traceback containing an
    interpreter path or a proxy endpoint.

    ``stop_event`` is how a supervising service asks for an orderly stop. It is the only external
    control this owner accepts, and setting it never interrupts the shutdown sequence itself.
    """
    deps = deps or OwnerDependencies()
    stop = stop_event or threading.Event()

    # 0. Exclusive per-user ownership, before anything is created. A refusal here is the ONLY
    #    correct answer when another owner is alive: answering through somebody else's Bridge would
    #    return plausible results for a lifecycle this process does not control.
    try:
        lease = deps.acquire_lease()
    except (LifecycleOwnershipError, OSError) as exc:
        _diagnostic(exc)
        return EXIT_REFUSED

    try:
        # 1-5. policy and environment, still before any kernel object exists.
        try:
            workdirs, settings = deps.load_config(Path(config_path))
            request = deps.render_request(workdirs, settings)
            rendered = (
                _render_config_pinned(request, bridge_python)
                if bridge_python is not None
                else deps.render_config(request)
            )
            proxy = deps.parse_proxy(settings.agent, environ)
            child_env = deps.build_env(proxy, source=environ)
            # The installed owner pins its own Bridge interpreter; an old
            # deployment override must not leak into provider descendants.
            child_env.pop("SERVERFS_BRIDGE_PYTHON", None)
        except Exception as exc:  # noqa: BLE001 - fail closed without leaking config/OS details
            _diagnostic(exc)
            return EXIT_REFUSED

        job = deps.make_job()
        bridge: subprocess.Popen[bytes] | None = None
        try:
            try:
                # 6. A taken job name means the previous containment object still exists, so
                #    nothing here may claim anything about its tree. Fails closed.
                job.open()
                # 7-8. Spawn suspended and assign inside spawn_bridge, before any material is sent.
                bridge = deps.spawn(
                    config_path=rendered.config_path,
                    child_env=child_env,
                    job=job,
                    argv=(
                        _pinned_bridge_argv(rendered.config_path, bridge_python)
                        if bridge_python is not None
                        else deps.bridge_argv(rendered.config_path)
                    ),
                )
                # 9. The containment fact, stated only now: this process holds the lease (so no
                #    earlier owner is alive) and it *created* the named job (so the object that held
                #    the previous provider tree is gone and Windows has ordered that tree to stop).
                #    Both halves are readings of kernel objects, not inferences from a task count.
                _send_bootstrap(bridge, proxy)
                # 10. Authenticated readiness. A pipe that exists but is owned by another identity
                #     is exactly the case this must not mistake for readiness.
                if bridge_python is not None:
                    deps.await_ready_bound(rendered.socket_path, bridge, job)
                else:
                    deps.await_ready(rendered.socket_path, bridge)
            except Exception as exc:  # noqa: BLE001 - a failed Bridge start must not serve
                _diagnostic(exc)
                return EXIT_REFUSED

            # 11. Own the lifetime. No stdio child, no HTTP Core, no Connector: those belong to
            #     other components and ending this owner must not end them.
            return _own_until_told_to_stop(bridge, stop, poll_interval)
        finally:
            _shutdown(bridge, job, deps)
    except Exception as exc:  # noqa: BLE001 - also covers job construction/close failure
        _diagnostic(exc)
        return EXIT_REFUSED
    finally:
        # Released last: the next owner must not see a free lease over a generation that has not
        # yet been ordered to stop.
        lease.close()


def _send_bootstrap(bridge: subprocess.Popen[bytes], proxy: object) -> None:
    """Write the single runtime-only frame. Only ever called after containment exists."""
    if bridge.stdin is None:
        raise AgentLifecycleError("the Bridge lifecycle pipe is unavailable")
    frame = bootstrap_frame_bytes(proxy, prior_bridge_execution_stopped=True)
    if bridge.stdin.write(frame) != len(frame):
        raise AgentLifecycleError("the Bridge lifecycle bootstrap was incomplete")
    bridge.stdin.flush()


def _own_until_told_to_stop(
    bridge: subprocess.Popen[bytes],
    stop: threading.Event,
    poll_interval: float,
) -> int:
    """Idle while the Bridge lives. Process-level liveness only.

    Responsiveness probing (``bridge ping``) is deliberately absent: it is a supervision policy that
    belongs to whatever service supervises this owner, and duplicating it here would give two
    components an opinion about restarting the same Bridge.
    """
    while not stop.is_set():
        code = bridge.poll()
        if code is not None:
            _diagnostic(f"the Agent Bridge exited (code {code})")
            return EXIT_BRIDGE_LOST
        time.sleep(poll_interval)
    return EXIT_OK


def _shutdown(
    bridge: subprocess.Popen[bytes] | None,
    job: WindowsJob,
    deps: OwnerDependencies,
) -> None:
    """Ask the Bridge to stop, wait a bounded time, then close the Job as the final kill.

    Each step is individually guarded so an exception in one cannot skip ``job.close()``: the close
    is what guarantees no provider descendant survives.
    """
    if bridge is not None:
        try:
            deps.request_stop(bridge)
            if not deps.await_exit(bridge):
                # Bounded wait expired. JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is the kill; no signal is
                # sent here and nothing blocks the close below.
                _diagnostic("the Agent Bridge did not exit within the graceful window")
        except Exception as exc:  # noqa: BLE001 - containment must still run
            _diagnostic(f"bridge shutdown failed ({type(exc).__name__})")
    try:
        job.close()
    except Exception as exc:  # noqa: BLE001 - the caller must report failure, lease still releases
        raise AgentLifecycleError(
            f"the containment job could not be closed ({type(exc).__name__})"
        ) from None


def main(argv: list[str] | None = None) -> int:
    """Narrow entrypoint for a future component-scoped service owner.

    One argument, a configuration path. No shell, no argv passthrough, no environment control: the
    process it may start is fixed by the native config and the renderer, not by the caller.
    """
    parser = argparse.ArgumentParser(
        prog="serverfs-bridge-owner",
        description="Own one independent Agent Bridge generation (Windows, same user).",
    )
    parser.add_argument("--config", required=True, help="Path to serverfs.toml")
    arguments = parser.parse_args(argv)
    return run(arguments.config)


__all__ = [
    "EXIT_BRIDGE_LOST",
    "EXIT_OK",
    "EXIT_REFUSED",
    "IDLE_POLL_SECONDS",
    "OwnerDependencies",
    "main",
    "run",
]

if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the service owner
    raise SystemExit(main())
