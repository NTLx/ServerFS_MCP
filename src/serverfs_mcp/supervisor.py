"""Minimal stdio supervisor that strips tunnel-only environment from ServerFS.

Keep the base module limited to stdlib imports: it runs inside tunnel-client's inherited environment
and must create the sanitized child environment before importing any ServerFS runtime module.

v0.11 Phase D adds an Agent-enabled path alongside the unchanged v0.10 one. The two are kept
deliberately separate rather than interleaved, because the disabled case is a hard upgrade gate:
with no ``[agent]`` section the supervisor must not create a Job Object, start a Bridge, create
Agent state, resolve a runtime binary or require a proxy. The agent work lives in
``agent_lifecycle`` and is entered only after the configuration says delegation is on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

#: Marker environment variable naming the Bridge interpreter. Read here, before any runtime import,
#: because the supervisor constructs the sanitized environment first.
BRIDGE_PYTHON_ENV = "SERVERFS_BRIDGE_PYTHON"


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    prefixes = ("CONTROL_PLANE_", "TUNNEL_CLIENT_", "OPENAI_", "MCP_", "SERVERFS_PROXY_")
    proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    return {
        key: value
        for key, value in env.items()
        if not key.upper().startswith(prefixes) and key.lower() not in proxy_names
    }


def _copy(source: BinaryIO, destination: BinaryIO, *, close_destination: bool = False) -> None:
    try:
        read = getattr(source, "read1", source.read)
        while chunk := read(64 * 1024):
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        if close_destination:
            try:
                destination.close()
            except OSError:
                pass


def forward_stdio(command: Sequence[str], env: dict[str, str] | None = None) -> int:
    """Start a separate child and forward its protocol streams transparently."""
    child = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=sanitized_environment() if env is None else env,
        bufsize=0,
    )
    assert child.stdin is not None and child.stdout is not None
    input_thread = threading.Thread(
        target=_copy,
        args=(sys.stdin.buffer, child.stdin),
        kwargs={"close_destination": True},
        daemon=True,
    )
    input_thread.start()
    try:
        _copy(child.stdout, sys.stdout.buffer)
        return child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="serverfs-supervisor")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    # Construct the sanitized environment before importing cli/runtime. The v0.10 path never looks
    # at the Agent configuration at all, so an installation without delegation cannot be affected by
    # anything in agent_lifecycle.
    env = sanitized_environment()

    agent_enabled = _agent_delegation_enabled(args.config)
    if not agent_enabled:
        command = [sys.executable, "-m", "serverfs_mcp.cli", "serve", "--config", args.config]
        return forward_stdio(command, env)
    if sys.platform == "darwin":
        # v0.13 topology (§12 E5): launchd owns the persistent Agent Bridge;
        # the stdio supervisor's job is only the sanitized environment plus
        # the ServerFS child, which connects to the Bridge's AF_UNIX endpoint
        # (derived, same address the launchd Bridge serves). The Windows
        # Agent path below starts and contains a Bridge child — that is the
        # Windows lifecycle, not a shape macOS should imitate.
        command = [sys.executable, "-m", "serverfs_mcp.cli", "serve", "--config", args.config]
        return forward_stdio(command, env)
    # Imported here, not at module scope, because this module must build its sanitized environment
    # before it imports any runtime module; a top-level import would defeat that ordering. The Agent
    # path is the only caller that needs it, so an installation without delegation never loads it.
    from .agent_lifecycle import AgentLifecycleError

    try:
        return run_with_agent(args.config, env)
    except AgentLifecycleError as exc:
        # The Agent path already redacts its own failures, so this is the last place a refusal can
        # be turned into a message rather than a traceback. Without it the exception escaped
        # ``main`` and every Agent startup failure reached the operator as a Python stack trace --
        # including
        # the filesystem paths and interpreter locations those messages deliberately omit.
        sys.stderr.write(f"serverfs-supervisor: {exc}\n")
        return 2


def _agent_delegation_enabled(config_path: str) -> bool:
    """Whether this configuration opts into Agent delegation.

    Read through the same loader the service uses, so the supervisor and the child cannot disagree
    about whether Agent is on. A config that fails to parse here falls back to the filesystem-only
    path and lets ``serve`` report the configuration error, which is the same operator experience a
    v0.10 install has.
    """
    try:
        from .native_config import load_native_config

        _workdirs, settings = load_native_config(Path(config_path))
        return settings.agent_enabled
    except Exception:
        return False


def run_with_agent(config_path: str, base_env: dict[str, str]) -> int:
    """The Agent-enabled lifecycle, in the frozen startup order (§15 D5).

    Implemented as an explicit sequence rather than a set of loosely-ordered helpers so that each
    step's precondition is visibly satisfied by the previous one. Any failure terminates the Bridge
    tree through the Job Object and reports a redacted error; the ServerFS stdio child is only
    started once the Bridge is genuinely ready.

    Step 0, new in Phase F, is per-user lifecycle ownership. The Named Pipe endpoint is derived from
    the user SID alone, so without it a second Agent-enabled supervisor would contend for the same
    pipe and its client would silently attach to the first chain's Bridge.

    The containment fact the Bridge reads out of the bootstrap frame has two halves, and they come
    from two different kernel objects on purpose. Acquiring the lease shows no earlier owner is
    alive -- but the lease never contained anything, so on its own it only makes a previous
    generation *likely* stopped. Creating a **fresh named Job** is the other half: a job object is
    destroyed once its last handle closes, and closing it is what delivers kill-on-close to every
    member, so finding the name free means the object that actually held the previous provider tree
    no longer exists and Windows has ordered all of it to stop. That is an "execution stopped"
    statement, not a "process objects destroyed" one -- termination finishes asynchronously -- and
    the frame says exactly that much. A name that is already taken fails closed, because then the
    old containment object is still there and nothing here can vouch for it.
    """
    from .agent_lifecycle import (
        AgentLifecycleError,
        agent_proxy_from_environment,
        await_graceful_exit,
        bootstrap_frame_bytes,
        bridge_child_environment,
        render_request_from_config,
        request_graceful_shutdown,
        spawn_bridge,
        terminate_process,
        wait_for_bridge_readiness,
    )
    from .native_config import load_native_config
    from .native_lifecycle import LifecycleLease, LifecycleOwnershipError, job_name
    from .windows_job import JobObjectError, WindowsJob

    # 0. per-user lifecycle ownership, before anything is created
    try:
        lease = LifecycleLease().acquire()
    except LifecycleOwnershipError as exc:
        # Fail closed rather than attaching to another chain's Bridge: answering through somebody
        # else's Bridge returns plausible results for the wrong lifecycle. Nothing was created yet,
        # so there is nothing to unwind.
        raise AgentLifecycleError(str(exc)) from exc

    try:
        # 1. load native config
        workdirs, settings = load_native_config(Path(config_path))

        # 2-3. resolve the data home and render the private Bridge configuration. The renderer
        # lives in the agent_bridge package (§23/§70), so the supervisor calls its CLI entry
        # point rather than importing across the package boundary.
        request = render_request_from_config(workdirs, settings)
        rendered = _render_bridge_config(request)

        # 4. parse the dedicated Agent proxy material (runtime-only, never persisted)
        proxy = agent_proxy_from_environment(settings.agent)

        # 5. build the scrubbed Bridge environment
        child_env = bridge_child_environment(proxy)

        job = WindowsJob(job_name())
        bridge: subprocess.Popen[bytes] | None = None
        stdio_child: subprocess.Popen[bytes] | None = None
        try:
            # 6-7. create the Job Object, then spawn the Bridge. A named job that already exists is
            # refused by `open()`: that means the previous generation's containment object is still
            # there, so nothing may be claimed about its tree.
            job.open()
            bridge = spawn_bridge(
                config_path=rendered.config_path,
                child_env=child_env,
                job=job,
                argv=_bridge_argv(rendered.config_path),
            )
            # 8. assignment happens inside spawn_bridge, before any material is sent

            # 9. one bounded bootstrap frame, after containment exists
            if bridge.stdin is not None:
                # The containment fact travels here and nowhere else: it is not a secret, but
                # it is a private parent-to-child lifecycle statement, so it belongs on this
                # channel and not in the public RPC. It is true here because the two steps above
                # made it true: this process holds the per-user lifecycle lease, and it *created*
                # the named Job Object rather than finding one -- so the object that held the old
                # provider tree is gone and Windows has ordered that tree to stop. "Stopped", not
                # "destroyed": termination finishes asynchronously, and the frame claims no more
                # than the recovery decision needs.
                bridge.stdin.write(
                    bootstrap_frame_bytes(proxy, prior_bridge_execution_stopped=True)
                )
                bridge.stdin.flush()

            # 10. authenticated readiness over the real Named Pipe
            import asyncio

            asyncio.run(wait_for_bridge_readiness(rendered.socket_path, bridge))

            # 11-12. start the stdio child with Agent wiring, then forward MCP frames
            stdio_child = _start_stdio_child(rendered, base_env, config_path)
            # A Bridge that dies while this supervisor lives would leave the Job and the per-user
            # lifecycle lease held by a chain that serves nothing. Ending the forwarding loop is
            # what runs the shutdown path below: Job close -- the kill for the provider tree -- and
            # then lifecycle release.
            bridge_watcher = _watch_bridge(bridge, stdio_child)
            try:
                return forward_stdio_child(stdio_child)
            finally:
                bridge_watcher.set()
        except (AgentLifecycleError, JobObjectError) as exc:
            # Both are redacted by construction: a failure class, never a raw handle or a traceback.
            # Containment cannot be established or verified, so an Agent-enabled startup must not
            # continue with a Bridge running outside the job (§15 D6).
            sys.stderr.write(f"serverfs-supervisor: {exc}\n")
            return 2
        finally:
            # §15 D7: stop the stdio child, ask the Bridge to stop, wait bounded, then close the Job
            # Object as the final containment.
            #
            # Every step is individually guarded so a failure in one cannot skip job.close():
            # closing the job is what guarantees no provider descendant survives, so it must
            # not be reachable-around by an exception in the step before it.
            if stdio_child is not None:
                terminate_process(stdio_child)
            if bridge is not None:
                try:
                    request_graceful_shutdown(bridge)
                    if not await_graceful_exit(bridge):
                        # The bounded wait expired. JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is the
                        # kill, so no signal is sent here and nothing blocks the close below.
                        pass
                except Exception as shutdown_error:  # noqa: BLE001 - containment must still run
                    sys.stderr.write(
                        "serverfs-supervisor: bridge shutdown failed "
                        f"({type(shutdown_error).__name__})\n"
                    )
            job.close()
    finally:
        # Released last, after job.close(): the next owner must not see a free lease and a free
        # job name over a generation whose members have not been told to stop.
        lease.close()


#: How often the supervisor checks whether its Bridge is still alive.
BRIDGE_WATCH_INTERVAL_SECONDS = 0.5


def _watch_bridge(
    bridge: subprocess.Popen[bytes], stdio_child: subprocess.Popen[bytes]
) -> threading.Event:
    """Terminate the stdio child when the Bridge exits, so a chain cannot outlive its Bridge.

    Phase F measured this gap: nothing monitored the Bridge, so a Bridge that died while the
    supervisor lived left the supervisor holding the Job Object and the lifecycle lease with a dead
    Bridge behind it -- a chain that serves nothing and blocks every later start. Ending the
    forwarding loop is what makes the ordinary shutdown path run: Job close, which is the kill for
    the provider tree, and then lifecycle release.
    """
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(BRIDGE_WATCH_INTERVAL_SECONDS):
            if bridge.poll() is not None:
                terminate_quietly(stdio_child)
                return

    threading.Thread(target=watch, daemon=True, name="bridge-watch").start()
    return stop


def _render_bridge_config(request: dict) -> _RenderedPaths:
    """Render the private Bridge config by invoking the Bridge-owned entry point.

    §23/§70 freeze the packages as independent, so this is a subprocess call rather than an import.
    It is also the safer shape for what the request contains: the document is handed over on the
    renderer's stdin and the only thing that comes back is the placement summary, so no intermediate
    copy of the request exists on disk.
    """
    import json

    from .agent_lifecycle import AgentLifecycleError

    argv = [
        os.environ.get(BRIDGE_PYTHON_ENV, "").strip() or sys.executable,
        "-m",
        "serverfs_agent_bridge.render_config",
    ]
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request).encode("utf-8"),
            capture_output=True,
            env=_bridge_render_env(),
        )
    except OSError as exc:
        raise AgentLifecycleError(
            f"the Bridge configuration could not be rendered ({type(exc).__name__})"
        ) from exc
    if completed.returncode != 0:
        # The renderer prints redacted refusals; its stderr is safe to surface as-is.
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise AgentLifecycleError(detail or "the Bridge configuration was refused")
    try:
        summary = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentLifecycleError("the Bridge renderer returned an unreadable summary") from exc
    return _RenderedPaths(summary)


class _RenderedPaths:
    """The placement facts the supervisor needs, as named attributes.

    Deliberately not the Bridge's own dataclass: this process must not import the Bridge package,
    so the summary is read structurally.
    """

    def __init__(self, summary: dict) -> None:
        self.config_path = Path(summary["config_path"])
        self.state_dir = Path(summary["state_dir"])
        self.lock_dir = Path(summary["lock_dir"])
        self.socket_path = Path(summary["socket_path"])
        self.allowed_peer_sid = summary["allowed_peer_sid"]
        self.lease_ids = tuple(summary.get("lease_ids", ()))


def _bridge_render_env() -> dict[str, str]:
    """The renderer's environment: fully scrubbed, then only SERVERFS_DATA_HOME re-added.

    The renderer runs in its own process and has no reason to see the Agent proxy endpoint, the
    Tunnel namespace or any proxy variable. ``SERVERFS_DATA_HOME`` is the single exception because
    the operator (or a test) chose where the private state lives, and the renderer must honour the
    same location the Bridge will use.

    The v0.10 ``sanitized_environment`` is deliberately not used here: it predates the Agent
    namespace and would pass ``SERVERFS_AGENT_PROXY_URL`` straight through. The Agent-enabled path
    uses the stricter scrub and then re-adds by name, so the set of variables that survive is
    exactly what is written below and nothing accumulates by accident.
    """
    from .agent_lifecycle import bridge_child_environment

    env = bridge_child_environment(None)
    override = os.environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        env["SERVERFS_DATA_HOME"] = override
    return env


def _bridge_argv(config_path: Path) -> list[str]:
    """The Bridge command line: only non-secret placement facts (§15 D2 argv scan)."""
    override = os.environ.get(BRIDGE_PYTHON_ENV, "").strip()
    python = override or sys.executable
    return [
        python,
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(config_path),
        "--supervised",
    ]


def _start_stdio_child(
    rendered, base_env: dict[str, str], config_path: str
) -> subprocess.Popen[bytes]:
    """Start the ServerFS stdio child with Agent wiring derived from the rendered config.

    The Agent policy itself reaches the child through its own parsed ``serverfs.toml``, so no
    operator-facing environment variable duplicates it (§15 D5 step 11).

    The environment is scrubbed rather than inherited, and the endpoint is deliberately *not* among
    the three values re-added. The stdio child registers the Agent surface and talks to the Bridge
    over the pipe; it has no reason to know where the runtime egress proxy points, and passing the
    endpoint to a process that does not need it is the same exposure Phase 0F measured — the value
    would be one ``os.environ`` read away from agent-executed tool code.
    """
    from .agent_lifecycle import AgentLifecycleError, bridge_child_environment

    env = bridge_child_environment(None)
    # Exactly three internal wiring values, re-added by name after the scrub.
    env["SERVERFS_AGENT_BRIDGE_ENABLED"] = "1"
    env["SERVERFS_AGENT_BRIDGE_SOCKET"] = str(rendered.socket_path)
    env["SERVERFS_AGENT_LOCK_DIR"] = str(rendered.lock_dir)
    del base_env  # the scrubbed environment is authoritative, not an overlay on the parent
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "serverfs_mcp.cli", "serve", "--config", config_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            env=env,
            bufsize=0,
        )
    except OSError as exc:
        raise AgentLifecycleError(
            f"the ServerFS stdio child could not be started ({type(exc).__name__})"
        ) from exc


def forward_stdio_child(child: subprocess.Popen[bytes]) -> int:
    """Forward MCP frames to an already-started child (the Agent path's stdio shape)."""
    assert child.stdin is not None and child.stdout is not None
    input_thread = threading.Thread(
        target=_copy,
        args=(sys.stdin.buffer, child.stdin),
        kwargs={"close_destination": True},
        daemon=True,
    )
    input_thread.start()
    try:
        _copy(child.stdout, sys.stdout.buffer)
        return child.wait()
    except BaseException:
        terminate_quietly(child)
        raise


def terminate_quietly(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            child.kill()
            child.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001 - the last line of defence for the launcher
        # Nothing may reach the operator as a traceback from this entry point: the process is
        # launched by a supervisor whose own diagnostics are the operator's only window into a
        # failure. The class name identifies the failure without leaking a path.
        sys.stderr.write(f"serverfs-supervisor: {type(_exc).__name__}\n")
        raise SystemExit(2) from None
