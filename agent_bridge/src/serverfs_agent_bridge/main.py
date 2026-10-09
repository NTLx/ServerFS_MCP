"""CLI entry point for the standalone Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
from pathlib import Path

from . import adapters as runtime_adapters
from .bootstrap import (
    BootstrapError,
    BootstrapFrame,
    parse_bootstrap_frame,
)
from .config import BridgeConfig
from .leases import LeaseManager
from .preflight import JevTaskPreflight
from .protocol import BridgeProtocolServer
from .service import BridgeLimits, BridgeService
from .store import TaskStore


async def _serve(
    config: BridgeConfig,
    *,
    shutdown_event: asyncio.Event | None = None,
    bootstrap: BootstrapFrame | None = None,
    supervised: bool = False,
) -> None:
    adapters = {}
    # The runtime-only egress material from the private bootstrap channel. It is handed to the
    # adapters here and applied downward to each runtime's network-owning child; it is never placed
    # in this process's environment, never persisted, and never logged (§15 D2/D4).
    runtime_proxy = bootstrap.agent_proxy if bootstrap is not None else None
    if config.enable_fake_runtime:
        # The fake runtime is given the same runtime-only material a real adapter would receive, so
        # the D2 tests exercise the real wiring rather than a parallel path.
        fake = runtime_adapters.FakeAdapter(runtime_proxy=runtime_proxy)
        adapters[fake.name] = fake
    if config.codex.enabled:
        # state_dir is the Bridge's private state root. On Windows the Bridge owns the Codex
        # app-server child, so its capability token lives under that root rather than in a
        # workdir; on Linux the adapter ignores it and keeps using the managed daemon's socket.
        codex = runtime_adapters.CodexAdapter(
            config.codex,
            runtime_proxy=runtime_proxy,
            state_dir=config.state_dir,
        )
        adapters[codex.name] = codex
    if config.claude.enabled:
        # The Claude SDK inherits this process's environment wholesale and layers options.env on
        # top, with no way to delete an inherited name. The endpoint therefore reaches the child
        # as an addition-only overlay built by the adapter (§7.2); the supervisor scrub of this
        # process's environment remains the boundary that keeps the inheritance clean.
        claude = runtime_adapters.ClaudeAdapter(config.claude, runtime_proxy=runtime_proxy)
        adapters[claude.name] = claude
    if config.qoder.enabled:
        # The Qoder SDK inherits the Bridge environment wholesale and applies an overlay on top, so
        # the endpoint cannot be handed to it the way Codex is: it arrives through `set_proxy()`
        # after connect, and the environment gets a deletion-only scrub instead.
        qoder = runtime_adapters.QoderAdapter(config.qoder, runtime_proxy=runtime_proxy)
        adapters[qoder.name] = qoder

    preflight = (
        JevTaskPreflight.from_api_key(config.jev.api_key)
        if config.jev.enabled and config.jev.api_key is not None
        else None
    )

    store = TaskStore(config.state_dir)
    service = BridgeService(
        store=store,
        policies=config.policies,
        adapters=adapters,
        lease_manager=LeaseManager(
            config.lock_dir,
            shared_gid=config.allowed_peer_gid,
            lease_ids=config.policies.lease_ids(),
        ),
        limits=BridgeLimits(
            task_timeout_seconds=config.limits.task_timeout_seconds,
            interaction_timeout_seconds=config.limits.interaction_timeout_seconds,
            max_active_tasks=config.limits.max_active_tasks,
            retention_seconds=config.limits.retention_seconds,
        ),
        preflight=preflight,
        # The supervisor's containment proof, over the private bootstrap channel only. Defaults to
        # False for an unsupervised launch, so recovery stays fail-closed unless containment was
        # actually demonstrated.
        prior_bridge_execution_stopped=(
            bootstrap.prior_bridge_execution_stopped if bootstrap is not None else False
        ),
    )
    await service.start()

    server = BridgeProtocolServer(
        service=service,
        socket_path=config.socket_path,
        allowed_peer_uid=config.allowed_peer_uid,
        allowed_peer_gid=config.allowed_peer_gid,
        allowed_peer_sid=config.allowed_peer_sid,
    )
    await server.start()

    own_shutdown_event = shutdown_event is None
    stop = shutdown_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    # A supervised Bridge takes its shutdown cue from the lifecycle pipe (§15 D4/D5). Windows
    # signal delivery is not a reliable control plane there, so the handlers below stay a
    # compatibility path for an unsupervised launch rather than the primary mechanism.
    # A supervised Bridge must not fall back to signal handling on Windows: the supervisor owns the
    # lifecycle and closes the pipe when it wants the Bridge to stop (§15 D4).
    if own_shutdown_event and not supervised and sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signals.append(sig)

    serve_task = asyncio.create_task(server.serve_forever())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {serve_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if serve_task in done:
            await serve_task
    finally:
        for task in (stop_task, serve_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stop_task, serve_task, return_exceptions=True)
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        await server.close()
        await service.close()


def unsupervised_refusal(*, supervised: bool, platform: str) -> str | None:
    """Why an unsupervised launch is refused on Windows, or ``None`` when it is allowed.

    The supervision is not merely a launch style on Windows: the supervisor is what holds the
    per-user lifecycle ownership and creates the named Job Object, and those two facts are what
    let a later start know that the previous generation's execution has stopped. An unsupervised
    Bridge has
    neither -- it holds no lease and sits outside any job -- yet it can run providers and create
    recovery guards just the same. If it died leaving an orphan, the next supervisor would acquire
    the lease, conclude containment, and clear a guard for a provider that is still running.

    So on Windows the only supported shape is the supervised one, and an unsupervised launch is a
    refusal rather than a degraded mode. Linux is untouched: the containment argument is Windows's,
    and its deployment shape is unchanged.

    Pure in its inputs so the decision can be pinned off-Windows; ``main`` passes the parsed flag
    and ``sys.platform``.
    """
    if platform != "win32" or supervised:
        return None
    return (
        "an unsupervised Bridge is not supported on Windows: without a supervisor there is no "
        "named Job Object and no per-user lifecycle ownership, so it cannot be shown that a "
        "provider tree it leaves behind has stopped running, and a later start would clear "
        "recovery state it cannot vouch for"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ServerFS Agent Bridge")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the Agent Bridge JSON configuration file",
    )
    parser.add_argument(
        "--supervised",
        action="store_true",
        help=(
            "run under a native supervisor: read one bounded runtime-only bootstrap frame from "
            "stdin, then treat stdin EOF as the graceful shutdown request (section 15 D4)"
        ),
    )
    args = parser.parse_args()

    # Refused before the configuration is read: the condition does not depend on it, and the answer
    # is the same whether the document is valid or not.
    refusal = unsupervised_refusal(supervised=args.supervised, platform=sys.platform)
    if refusal is not None:
        print(f"bridge refused: {refusal}", file=sys.stderr)
        raise SystemExit(2)

    try:
        config = BridgeConfig.load(args.config)
    except (OSError, ValueError) as exc:
        # Configuration refusals are redacted by construction and name no secret.
        print(f"bridge configuration refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if not args.supervised:
        asyncio.run(_serve(config))
        return

    asyncio.run(_serve_supervised(config))


async def _serve_supervised(config: BridgeConfig) -> None:
    """The supervised lifecycle: one bootstrap frame in, pipe-EOF shutdown out.

    The frame is read before anything is started, so a malformed or absent handshake cannot leave a
    half-configured Bridge running. The same pipe then stays open and its EOF is the shutdown cue,
    which reuses ``_serve``'s existing close path rather than adding a second teardown (§15 D4).

    stdin is read on a worker thread rather than through ``loop.connect_read_pipe``. That is a
    measured platform constraint, not a preference: on Windows the default Proactor loop does not
    deliver data from a connected pipe and its transport raises on close, so an asyncio reader
    silently never sees the frame. A blocking read on a thread is the same pipe and the same
    one-frame contract — §15 D4 forbids switching the *transport* to an environment variable or a
    config file, and this does not.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _pump() -> None:
        try:
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                loop.call_soon_threadsafe(inbox.put_nowait, line)
        except (OSError, ValueError):
            pass
        finally:
            loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=_pump, name="serverfs-bootstrap-stdin", daemon=True).start()

    async def _first_frame() -> BootstrapFrame:
        line = await inbox.get()
        if line is None:
            raise BootstrapError(
                "the supervisor closed the bootstrap channel before sending a frame"
            )
        return parse_bootstrap_frame(line)

    try:
        frame = await _first_frame()
    except BootstrapError as exc:
        # The message names the failure class only; the frame's contents never reach stderr.
        print(f"bootstrap refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # The same queue then carries the shutdown cue: EOF is the supervisor asking for a graceful
    # stop, which is simpler and more reliable than designing a second control protocol.
    shutdown = asyncio.Event()

    async def _watch_eof() -> None:
        while True:
            item = await inbox.get()
            if item is None:
                shutdown.set()
                return
            # Anything after the bootstrap frame is ignored: this channel carries exactly one
            # frame, so a second instruction cannot arrive on it.

    asyncio.ensure_future(_watch_eof())
    await _serve(config, shutdown_event=shutdown, bootstrap=frame, supervised=True)


if __name__ == "__main__":
    main()
