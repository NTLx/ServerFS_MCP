"""Test-only provider adapter for the D9 native lifecycle acceptance.

D9 must prove a *real* lifecycle: real supervisor, real renderer, real Bridge process, real Named
Pipe, real MCP surface, real writer lease. The one thing it must not do is talk to OpenAI, so the
provider adapter at the very end of that chain is replaced by a deterministic fake. Everything
    before
it is the production implementation.

The replacement is a **sitecustomize**, not a code change. The Bridge is launched by the supervisor
as ``python -m serverfs_agent_bridge.main``, and there is no flag this repository could add to that
without adding an operator-facing surface -- which the D9 contract forbids. A sitecustomize on
``PYTHONPATH`` intercepts the adapter constructor before ``main`` runs, and it exists only inside
    the
test process tree.

What it replaces and what it keeps:

- replaces: ``adapters.CodexAdapter`` -- the object that would spawn a real ``codex app-server``.
- keeps: the Bridge's config load, workdir policy, task store, lease manager, IPC, service and every
  other adapter protocol method. The fake subclasses the real ``AgentAdapter``, so the service
      drives
  it through exactly the code path a native runtime takes, including ``context.cwd`` resolution and
  ``context.profile``.

The public runtime name stays ``codex``, so the MCP surface is unchanged and the frozen ten-tool
contract is exercised rather than bypassed.

``SERVERFS_TEST_BRIDGE_MODE`` selects what the fake observes, because three lifecycle properties
    need
three different observations and none of them belongs in production:

``write``
    Create one fixed file under ``context.cwd`` and answer with a fixed string. Proves a real
    provider-side mutation of the authorized workspace.
``wait``
    Write a marker file, then park until cancelled. The marker is what makes the wait deterministic:
    it distinguishes "the turn actually started" from "the task was accepted", which a status poll
    cannot.
``env``
    Spawn a real child process and read *its* ``os.environ``. The capture comes from a separate
    process, so it cannot be satisfied by a helper returning the right value without anything
        calling
    it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKSPACE_WRITE_FILE = "phase-d-native-lifecycle.txt"
WORKSPACE_WRITE_BYTES = b"written by the D9 fake provider adapter\n"
WAIT_FILE = "phase-d-native-lifecycle-wait.txt"
#: Written only in the `approval` mode, after the real `request_approval` round trip resolves.
APPROVAL_ARTIFACT = "reproducer-artifact.txt"
ENV_CAPTURE_FILE = "phase-d-native-lifecycle-env.json"
#: Written only in the `child` mode: the pid of a real, long-lived descendant of the Bridge. The
#: fake runtime otherwise runs in-process, so without it the chain has no provider-shaped process
#: and a containment assertion about one would be about nothing.
PROVIDER_CHILD_FILE = "phase-d-provider-child.json"


def _install() -> None:
    from serverfs_agent_bridge import adapters as runtime_adapters
    from serverfs_agent_bridge.adapters.base import AdapterResult, AgentAdapter

    class D9FakeCodexAdapter(AgentAdapter):
        """A deterministic stand-in driven through the real adapter protocol."""

        def __init__(self, settings, **kwargs: Any) -> None:
            self.settings = settings
            self._cancelled: set[str] = set()
            # Accepted and ignored, exactly as the production fake runtime does: the point is that
            # the supervisor's wiring is uniform, not that this adapter needs the material.
            self._runtime_proxy = kwargs.get("runtime_proxy")
            # Read from the *rendered* config, never from a test-supplied value, so the proxy
            # observation below exercises the same flag production would read.
            self.use_proxy = bool(getattr(settings, "use_proxy", False))

        @property
        def name(self) -> str:
            # The public runtime name is unchanged, so the MCP surface and the ten frozen tools are
            # exercised against a runtime the operator really could have configured.
            return "codex"

        async def probe(self) -> Any:
            from serverfs_agent_bridge.models import RuntimeInfo

            # Only real RuntimeInfo fields. An earlier version passed ``detail=``, which is not a
            # field, so the constructor raised and list_runtimes reported the runtime as unavailable
            # -- a failure that looked exactly like "the Bridge is not wired up".
            return RuntimeInfo(
                name=self.name,
                available=True,
                version="d9-test",
                persistent_session=True,
                live_steer=False,
                interactive_approval=True,
                interactive_question=True,
                model_override=True,
            )

        async def run_task(self, context) -> AdapterResult:
            mode = os.environ.get("SERVERFS_TEST_BRIDGE_MODE", "write")
            # Emitted through the real context callback, so the event reaches the store by the
            # production path rather than being injected into the reader's answer.
            await context.emit_event("turn.started", {"runtime": self.name})

            if mode == "wait":
                (Path(context.cwd) / WAIT_FILE).write_bytes(b"waiting\n")
                while context.task_id not in self._cancelled:
                    await asyncio.sleep(0.02)
                raise asyncio.CancelledError

            if mode == "approval":
                # A genuine approval through the production path: `request_approval` is the real
                # context callback, so the Bridge creates the pending request, publishes it through
                # `get_agent_task`, and resumes this coroutine only when the answer arrives. That
                # makes the whole waiting state deterministic -- no provider, no network, no timing.
                await context.emit_event("item.started", {"runtime": self.name, "kind": "command"})
                resolution = await context.request_approval(
                    {
                        "category": "command",
                        "title": "Deterministic approval for the stall reproducer",
                        "command_display": "write reproducer-artifact.txt",
                        "available_decisions": [
                            "approve_once",
                            "approve_session",
                            "deny",
                            "cancel_task",
                        ],
                    }
                )
                decision = str(resolution.get("decision"))
                await context.emit_event("approval.observed", {"decision": decision})
                if decision == "cancel_task":
                    raise asyncio.CancelledError
                (Path(context.cwd) / APPROVAL_ARTIFACT).write_bytes(b"approval-roundtrip-ok\n")
                await context.emit_event("turn.completed", {"runtime": self.name})
                return AdapterResult(final_response=f"approval={decision}")

            if mode == "env":
                detail = await _capture_child_environment_async(
                    context.cwd, self.use_proxy, self._runtime_proxy
                )
                await context.emit_event("turn.completed", {"runtime": self.name})
                return AdapterResult(final_response=f"captured: {detail}")

            if mode == "child":
                # A Bridge-owned descendant that outlives the turn, so the chain has something a Job
                # Object is actually supposed to contain. The descriptor handles are DEVNULL for the
                # reason the env capture uses them: this child must not inherit the Bridge's stdout,
                # which is a pipe into the supervisor's forwarding loop.
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time;time.sleep(600)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                record = _record_dir()
                if record is not None:
                    (record / PROVIDER_CHILD_FILE).write_text(
                        json.dumps({"pid": child.pid}), encoding="utf-8"
                    )
                await context.emit_event("turn.completed", {"runtime": self.name})
                return AdapterResult(final_response="provider-child-spawned")

            if context.profile != "workspace-write":
                # A fake that wrote regardless of profile would prove nothing about the profile, and
                # the profile is the thing the writer lease exists to guard.
                raise RuntimeError("the D9 fake adapter only writes under workspace-write")

            (Path(context.cwd) / WORKSPACE_WRITE_FILE).write_bytes(WORKSPACE_WRITE_BYTES)
            await context.emit_event(
                "agent.message", {"runtime": self.name, "text": "workspace written"}
            )
            await context.emit_event("turn.completed", {"runtime": self.name})
            return AdapterResult(final_response=f"d9-fake-provider:{context.prompt}")

        async def get_state(self, task_id: str) -> str | None:
            return None

        async def reconcile_task(self, task) -> Any:
            """Report that no provider state outlives the call, which is true by construction.

            Without this the service cannot clear the recovery guard after a cancellation: it asks
            the runtime whether a provider is still active, and the base class answers "unknown",
                which
            the service correctly treats as "keep the guard". That is the right fail-closed default
                --
            a runtime that genuinely might have an orphan must keep the workdir locked -- so the
                test
            runtime has to answer, exactly as a real adapter does.
            """
            from serverfs_agent_bridge.adapters.base import ReconcileResult
            from serverfs_agent_bridge.models import ReconciliationStatus

            return ReconcileResult(
                status=ReconciliationStatus.NOT_RECOVERABLE,
                provider_active=False,
                detail="the D9 test runtime has no out-of-process provider state",
            )

        async def send_message(self, task_id: str, message: str) -> None:
            return None

        async def respond_approval(self, task_id: str, request_id: str, resolution: dict) -> None:
            return None

        async def answer_question(self, task_id: str, request_id: str, answer: dict) -> None:
            return None

        async def cancel(self, task_id: str) -> None:
            self._cancelled.add(task_id)

        async def close(self) -> None:
            return None

    runtime_adapters.CodexAdapter = D9FakeCodexAdapter  # type: ignore[misc,assignment]


def _record_dir() -> Path | None:
    raw = os.environ.get("SERVERFS_TEST_RECORD_DIR")
    return Path(raw) if raw else None


def _capture_child_environment(cwd: Path, use_proxy: bool, proxy: Any) -> str:
    """Spawn a real child under the production policy and read back its own environment.

    Reading ``os.environ`` in this process would only prove the Bridge's own environment, which is
        the
    wrong question: the trust boundary is the provider child, and only a separate process can show
    what that child actually received.

    The child itself is given ``NO_PROXY=*`` so that anything *it* does internally cannot try to
        reach
    the unreachable test endpoint through the proxy the test just injected. That does not affect
        what
    the child *observes*: it snapshots its own environment before doing anything else, and only the
    observed mapping is asserted on.
    """
    from serverfs_agent_bridge.runtime_proxy import build_runtime_environment

    child_env = build_runtime_environment(
        os.environ, runtime="codex", use_proxy=use_proxy, proxy=proxy
    )
    child_env["NO_PROXY"] = "*"
    child_env["no_proxy"] = "*"
    captured = subprocess.run(
        [sys.executable, "-I", "-c", "import json,os;print(json.dumps(dict(os.environ)))"],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(cwd),
        timeout=30,
        check=False,
        # The Bridge's own stdout is a pipe into the supervisor's forwarding loop. A child that
        # inherited it would write into a stream nobody drains on this path, and the read here would
        # block forever -- which is exactly what the first version of this harness did.
        stdin=subprocess.DEVNULL,
    )
    if captured.returncode != 0:
        return f"capture-failed rc={captured.returncode}"
    environment = json.loads(captured.stdout) if captured.stdout.strip() else {}
    # The child's own terminating override is replaced with what the *policy* produced, so the
    # recorded mapping describes what the real policy handed the child. Both are the same variable;
    # only the terminating value differed.
    if use_proxy:
        environment["NO_PROXY"] = (proxy.no_proxy if proxy is not None else "") or ""
        environment.pop("no_proxy", None)
    else:
        for name in ("NO_PROXY", "no_proxy"):
            environment.pop(name, None)
    record = _record_dir()
    if record is not None:
        (record / ENV_CAPTURE_FILE).write_text(
            json.dumps(environment, indent=2, sort_keys=True), encoding="utf-8"
        )
    return f"{len(environment)} variables"


async def _capture_child_environment_async(cwd: Path, use_proxy: bool, proxy: Any) -> str:
    """The same capture, run off the event loop **and** off the default executor.

    Two separate hazards, both hit by the first version of this harness.

    A synchronous ``subprocess.run`` inside ``run_task`` blocks the Bridge's whole event loop, so
        every
    other RPC -- including the ``get_agent_task`` poll a client is waiting on -- stalls until the
        client
    times out.

    Moving it to ``run_in_executor(None, ...)`` fixes that and creates a worse one: the Bridge's own
    pipe server uses the *default* executor to join connections
    (``windows_ipc`` -> ``run_in_executor(None, thread.join, ...)``), so a slow capture there
        starves
    the IPC server and the Bridge stops answering entirely. A dedicated thread has neither problem.
    """
    return await asyncio.to_thread(_capture_child_environment, cwd, use_proxy, proxy)


def _sitecustomize() -> None:
    """Entry point. Silent no-op unless this process was launched for a D9 acceptance test."""
    if os.environ.get("SERVERFS_TEST_BRIDGE_MODE") is None:
        return
    try:
        _install()
    except Exception:  # noqa: BLE001 - a harness must not crash the interpreter it patches
        import traceback

        traceback.print_exc(file=sys.stderr)


def _ensure_importable() -> None:
    """Put the Bridge distribution on ``sys.path`` before trying to patch it.

    A ``sitecustomize`` runs during ``site`` initialisation, which is *before* ``.pth`` files are
    processed. The Bridge virtualenv installs this package as an editable ``.pth``, so at the moment
    this file executes the distribution is not importable yet even though it is installed. Appending
    the source directory explicitly is what makes the patch possible at all.
    """
    src = os.environ.get("SERVERFS_TEST_BRIDGE_SRC")
    if src and src not in sys.path:
        sys.path.append(src)


def _bootstrap() -> None:
    if os.environ.get("SERVERFS_TEST_BRIDGE_MODE") is None:
        return
    _ensure_importable()
    _sitecustomize()


_bootstrap()
