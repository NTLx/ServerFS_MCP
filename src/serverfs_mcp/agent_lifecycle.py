"""Native Agent Bridge lifecycle under the ServerFS supervisor (v0.11 Phase D, §9.1, §15 D5/D7).

This is the Agent-enabled half of the supervisor. The Agent-disabled half is untouched: it is the
v0.10 code path in ``supervisor.py``, and nothing here is reached unless ``[agent]`` is enabled.

The startup order is frozen and implemented as written, because each step depends on the previous
one having already succeeded:

1. load native config 2. resolve the data home 3. render the private Bridge config
4. parse the dedicated Agent proxy material 5. build the scrubbed Bridge environment
6. create the Job Object 7. spawn the Bridge 8. assign it to the Job 9. send the bootstrap frame
10. wait for **authenticated** readiness 11. start the ServerFS stdio child 12. forward MCP stdio

Two properties are the reason this is a module rather than a few more branches in ``supervisor.py``:

- **Readiness is measured, never slept on.** A ``sleep(1)`` and a hopeful pipe check would make a
  slow machine fail intermittently and a fast one pass by luck. Readiness is a real
  ``runtime.list`` over the Named Pipe through ``AgentBridgeClient``, so the transport is up *and*
  the server SID matched (§4.4). A Bridge that exits first fails immediately instead of
  waiting out the timeout.
- **Failure leaves nothing running.** Any step failing terminates the Bridge tree through the Job
  Object, reports a redacted error, and never starts the ServerFS child. A partially-started Agent
  service would hold a writer lease nobody owns.

Shutdown is the reverse and is bounded (§15 D7): stop the stdio child, ask the Bridge to stop by
closing the lifecycle pipe, wait a bounded time for a graceful exit, then close the Job Object as
the final kill. The Job close is what guarantees no provider descendant survives.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .agent_client import AgentBridgeClient, AgentBridgeUnavailable
from .agent_proxy import (
    AgentProxyConfig,
    AgentProxyError,
    bridge_environment,
    parse_agent_proxy,
)

#: Bounded readiness wait. Generous enough for a cold SQLite store on a slow disk, short enough that
#: a dead Bridge surfaces quickly; the process-exit check is what actually ends the wait early.
READINESS_TIMEOUT_SECONDS = 30.0
READINESS_INTERVAL_SECONDS = 0.1

#: Bounded graceful-shutdown wait before the Job Object becomes the final kill (§15 D7). A single
#: internal constant: this is not an operator tuning knob, and an operator knob here would be a way
#: to make the containment weaker without saying so.
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class AgentLifecycleError(Exception):
    """Agent-enabled startup or shutdown failed; the message is redacted by construction."""


@dataclass(frozen=True)
class BridgeLaunch:
    """A started, contained, ready Bridge process."""

    process: subprocess.Popen[bytes]
    job: object
    config_path: Path
    socket_path: Path
    lock_dir: Path
    state_dir: Path


def _bridge_python() -> list[str]:
    """The interpreter that can import ``serverfs_agent_bridge``.

    The Bridge is a separate distribution (§23), so it may live in its own environment. Resolution
    is explicit and its failure is an ordinary startup error rather than an import deep inside a
    subprocess.
    """
    override = os.environ.get("SERVERFS_BRIDGE_PYTHON", "").strip()
    if override:
        return [override]
    return [sys.executable]


def render_request_from_config(workdirs, settings) -> dict:
    """Turn the parsed native config into the Bridge renderer's input document.

    Only non-secret policy crosses here. The proxy endpoint is deliberately absent: it is handed to
    the Bridge over the bootstrap channel and never becomes part of a persisted config (§15 D2/D4).
    """
    agent = settings.agent
    if agent is None or not agent.enabled:
        raise AgentLifecycleError("the native configuration does not enable Agent delegation")
    workdir_entries = []
    for workdir in workdirs:
        if workdir.agent_mode == "disabled" and not workdir.agent_runtimes:
            continue
        workdir_entries.append(
            {
                "alias": workdir.alias,
                "host_path": str(workdir.root),
                "read_only": workdir.read_only,
                "agent_mode": workdir.agent_mode,
                "agent_runtimes": sorted(workdir.agent_runtimes),
            }
        )
    if not workdir_entries:
        raise AgentLifecycleError(
            "Agent delegation is enabled but no workdir configures an agent_mode"
        )
    return {
        "workdirs": workdir_entries,
        # Per-runtime non-secret policy, not just names: the executable name and use_proxy both have
        # to survive into bridge.json, because use_proxy is what decides whether a provider child
        # receives the Agent proxy at all. Sending names alone silently dropped both.
        "runtimes": {
            name: {
                "enabled": True,
                _BIN_KEY[name]: agent.runtime_binary(name),
                "use_proxy": agent.runtime_use_proxy(name),
            }
            for name in sorted(agent.enabled_runtimes)
        },
        "limits": {
            "task_timeout_seconds": agent.task_timeout_seconds,
            "interaction_timeout_seconds": agent.interaction_timeout_seconds,
            "max_active_tasks": agent.max_active_tasks,
            "retention_seconds": agent.retention_seconds,
        },
    }


#: Each runtime's executable key in the rendered document, matching the Bridge's own field names.
_BIN_KEY = {"codex": "codex_bin", "claude": "claude_bin", "qoder": "qoder_bin"}


def bridge_child_environment(
    proxy: AgentProxyConfig | None,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment the Bridge process starts with (§15 D5 step 5).

    Scrubbing happens *here*, before the Bridge exists, because that is the only point at which it
    is guaranteed: Phase 0F measured that both provider SDKs copy the Bridge environment wholesale
    into their CLI child and that Claude's SDK cannot unset an inherited name, so a credential that
    reaches the Bridge would reach agent-executed tool code with no way to remove it afterwards.

    The endpoint is not placed in the environment either. It travels on stdin, so the Bridge process
    environment contains no proxy variable and no Agent namespace at all.
    """
    env = bridge_environment(source)
    # SERVERFS_AGENT_PROXY_* is already removed by the shared scrub; this assert documents that the
    # property is load-bearing rather than incidental.
    assert not any(name.upper().startswith("SERVERFS_AGENT_") for name in env)
    return env


def bootstrap_frame_bytes(proxy: AgentProxyConfig | None) -> bytes:
    """Encode the single runtime-only frame the Bridge reads from stdin."""
    document: dict[str, object] = {"version": 1}
    if proxy is not None:
        document["agent_proxy"] = {
            "enabled": True,
            "url": proxy.url,
            "no_proxy": proxy.no_proxy,
        }
    else:
        document["agent_proxy"] = {"enabled": False}
    return json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"


def spawn_bridge(
    *,
    config_path: Path,
    child_env: Mapping[str, str],
    job=None,
    argv: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start the Bridge supervised, with stdin as the lifecycle pipe (§15 D5 steps 7-8).

    stdin is a PIPE and nothing is written to it here: the caller sends the bootstrap frame only
    after containment is established, so a Bridge can never read runtime material before the Job
    Object exists. stdout/stderr are inherited so Bridge diagnostics reach the operator's stderr
    without being captured and withheld.
    """
    command = argv or [
        *_bridge_python(),
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(config_path),
        "--supervised",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            env=dict(child_env),
        )
    except OSError as exc:
        # The message names the failure class only; the environment is never echoed.
        raise AgentLifecycleError(
            f"the Agent Bridge could not be started ({type(exc).__name__})"
        ) from exc
    if job is not None:
        # Containment before any material: if assignment fails, nothing was ever sent to the child.
        job.assign(process)
    return process


async def wait_for_bridge_readiness(
    socket_path: Path,
    process: subprocess.Popen[bytes],
    *,
    timeout: float = READINESS_TIMEOUT_SECONDS,
) -> None:
    """Block until the Bridge answers an authenticated ``runtime.list`` (§15 D5 step 10).

    This is the real production path rather than a proxy for it: connecting to the Named Pipe proves
    the listener is up, and ``AgentBridgeClient`` verifies the server SID matches this user's before
    the call is accepted (§4.4). A pipe that exists but is owned by another identity is exactly the
    case that must not be mistaken for readiness.

    A Bridge that has already exited fails immediately rather than waiting out the timeout, so a
    configuration refusal surfaces as itself instead of as a generic startup timeout.
    """
    client = AgentBridgeClient(socket_path, timeout_seconds=timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise AgentLifecycleError(
                f"the Agent Bridge exited before becoming ready (code {exit_code})"
            )
        try:
            await client.call("runtime.list", {})
            return
        except (AgentBridgeUnavailable, OSError) as exc:
            last_error = exc
            await asyncio.sleep(READINESS_INTERVAL_SECONDS)
    raise AgentLifecycleError(
        f"the Agent Bridge did not become ready within {timeout:.0f}s "
        f"({type(last_error).__name__ if last_error else 'no response'})"
    )


def request_graceful_shutdown(process: subprocess.Popen[bytes]) -> None:
    """Ask the Bridge to stop by closing the lifecycle pipe (§15 D7 step 2).

    EOF is the agreed shutdown signal (§15 D4). Closing stdin is the whole request: there is no
    second control protocol to design, and it works identically on every platform.
    """
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            # A pipe already gone means the Bridge is already shutting down.
            pass


def await_graceful_exit(
    process: subprocess.Popen[bytes],
    *,
    timeout: float = GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
) -> bool:
    """Wait a bounded time for a clean exit. Returns False if the timeout expired.

    Never infinite: an unbounded wait here would turn a Bridge that ignores the shutdown request
    into a hung supervisor, which is the failure mode the Job Object exists to make survivable.
    """
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def terminate_process(process: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    """Best-effort termination for the stdio child, which is not Job-contained."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def agent_proxy_from_environment(settings, env: Mapping[str, str] | None = None):
    """Parse the dedicated Agent proxy material for the enabled configuration.

    A malformed or credential-bearing endpoint fails the startup here rather than producing a Bridge
    that silently runs direct.
    """
    try:
        return parse_agent_proxy(settings.proxy if settings else None, env)
    except AgentProxyError as exc:
        raise AgentLifecycleError(str(exc)) from exc


__all__ = [
    "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
    "READINESS_TIMEOUT_SECONDS",
    "AgentLifecycleError",
    "BridgeLaunch",
    "agent_proxy_from_environment",
    "await_graceful_exit",
    "bootstrap_frame_bytes",
    "bridge_child_environment",
    "render_request_from_config",
    "request_graceful_shutdown",
    "spawn_bridge",
    "terminate_process",
    "wait_for_bridge_readiness",
]
