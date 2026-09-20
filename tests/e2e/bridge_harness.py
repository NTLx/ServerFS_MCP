"""Bridge-side process for the two-process MCP <-> Bridge E2E harness.

Run with the **agent_bridge** environment; the driver (``run_e2e.py``) launches
it as a subprocess and speaks to it over a real AF_UNIX socket:

    agent_bridge/.venv/bin/python tests/e2e/bridge_harness.py \
        --socket ... --lock-dir ... --state-dir ... --workdir ...

This is an integration harness, not production behaviour. It maps the public
runtime name ``codex`` onto the deterministic ``FakeAdapter`` so the MCP
surface -- whose public allowlist is exactly codex/claude -- can be driven end
to end without a provider. The production ``FakeAdapter`` keeps
``name == "fake"`` and is never added to the MCP runtime allowlist.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

WORKDIR_ALIAS = "repo"
READY_LINE = "BRIDGE_READY"


class CodexNamedFakeAdapter(FakeAdapter):
    """Test-only runtime mapping: public name ``codex`` runs the fake adapter."""

    @property
    def name(self) -> str:
        return "codex"


async def _serve(args: argparse.Namespace) -> None:
    policy = WorkdirAgentPolicy(
        slot=1,
        alias=WORKDIR_ALIAS,
        host_path=args.workdir,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"codex"}),
        read_only=False,
    )
    service = BridgeService(
        store=TaskStore(args.state_dir),
        policies=PolicyRegistry([policy]),
        adapters={"codex": CodexNamedFakeAdapter()},
        lease_manager=LeaseManager(args.lock_dir),
    )
    await service.start()
    server = BridgeProtocolServer(service=service, socket_path=args.socket)
    await server.start()
    print(READY_LINE, flush=True)
    try:
        await server.serve_forever()
    finally:
        await server.close()
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge process for the MCP E2E harness")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--lock-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
