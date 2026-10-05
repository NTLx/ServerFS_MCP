"""CLI entry point for the standalone Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from . import adapters as runtime_adapters
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
) -> None:
    adapters = {}
    if config.enable_fake_runtime:
        fake = runtime_adapters.FakeAdapter()
        adapters[fake.name] = fake
    if config.codex.enabled:
        codex = runtime_adapters.CodexAdapter(config.codex)
        adapters[codex.name] = codex
    if config.claude.enabled:
        claude = runtime_adapters.ClaudeAdapter(config.claude)
        adapters[claude.name] = claude
    if config.qoder.enabled:
        qoder = runtime_adapters.QoderAdapter(config.qoder)
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
    if own_shutdown_event:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="ServerFS Agent Bridge")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the Agent Bridge JSON configuration file",
    )
    args = parser.parse_args()
    config = BridgeConfig.load(args.config)
    asyncio.run(_serve(config))


if __name__ == "__main__":
    main()
