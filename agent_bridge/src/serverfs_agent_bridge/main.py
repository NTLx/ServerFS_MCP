"""CLI entry point for the standalone Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter, FakeAdapter
from .config import BridgeConfig
from .leases import LeaseManager
from .protocol import BridgeProtocolServer
from .service import BridgeService
from .store import TaskStore


async def _serve(
    config: BridgeConfig,
    *,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    adapters = {}
    if config.enable_fake_runtime:
        fake = FakeAdapter()
        adapters[fake.name] = fake
    if config.codex.enabled:
        codex = CodexAdapter(config.codex)
        adapters[codex.name] = codex
    if config.claude.enabled:
        claude = ClaudeAdapter(config.claude)
        adapters[claude.name] = claude

    store = TaskStore(config.state_dir)
    service = BridgeService(
        store=store,
        policies=config.policies,
        adapters=adapters,
        lease_manager=LeaseManager(config.lock_dir, shared_gid=config.allowed_peer_gid),
    )
    await service.start()

    server = BridgeProtocolServer(
        service=service,
        socket_path=config.socket_path,
        allowed_peer_uid=config.allowed_peer_uid,
        allowed_peer_gid=config.allowed_peer_gid,
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
