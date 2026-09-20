"""CLI entry point for the standalone Agent Bridge."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter, FakeAdapter
from .config import BridgeConfig
from .leases import LeaseManager
from .protocol import BridgeProtocolServer
from .service import BridgeService
from .store import TaskStore


async def _serve(config: BridgeConfig) -> None:
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
        lease_manager=LeaseManager(config.lock_dir),
    )
    await service.start()

    server = BridgeProtocolServer(
        service=service,
        socket_path=config.socket_path,
        allowed_peer_uid=config.allowed_peer_uid,
        allowed_peer_gid=config.allowed_peer_gid,
    )
    await server.start()
    try:
        await server.serve_forever()
    finally:
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
