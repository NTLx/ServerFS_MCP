"""Bridge-side process for the two-process MCP <-> Bridge E2E harness.

Run with the **agent_bridge** environment; the driver (``run_e2e.py``) launches
it as a subprocess and speaks to it over the platform's local endpoint — a real AF_UNIX socket
on Linux, a real Named Pipe on Windows:

    agent_bridge/.venv/bin/python tests/e2e/bridge_harness.py \
        --socket ... --lock-dir ... --state-dir ... --workdir ...

This is an integration harness, not production behaviour. It maps the public
runtime name ``codex`` onto the deterministic ``FakeAdapter`` so the MCP
surface -- whose public allowlist is exactly codex/claude/qoder -- can be driven end
to end without a provider. The production ``FakeAdapter`` keeps
``name == "fake"`` and is never added to the MCP runtime allowlist.

``--read-only`` serves a review workdir instead of a workspace-write one: a review task needs no
writer lease, so the driver can exercise the Agent lifecycle without contending for the workdir.
The lease artifact is pre-created here for exactly the one lease key this process will use, and the
key follows the platform's own deployment shape (§5.3) — a native deployment has no slots, and the
ServerFS reader beside it derives the alias-based name.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeLimits, BridgeService
from serverfs_agent_bridge.store import TaskStore

WORKDIR_ALIAS = "repo"
READY_LINE = "BRIDGE_READY"


class CodexNamedFakeAdapter(FakeAdapter):
    """Test-only runtime mapping: a chosen public runtime name runs the fake adapter.

    ``list_runtimes`` reports ``adapter.name``, and ``submit_task`` looks the adapter up by that
    same string, so the mapping has to change the name and not only the registry key. The default
    stays ``codex`` for the MCP-surface driver, whose public allowlist is codex/claude/qoder; a
    raw-RPC driver passes ``fake`` so it can submit the review profile, which the frozen contract
    refuses for a native runtime name.
    """

    def __init__(self, name: str = "codex") -> None:
        super().__init__()
        self._name = name

    @property
    def name(self) -> str:
        return self._name


async def _serve(args: argparse.Namespace) -> None:
    policy = WorkdirAgentPolicy(
        slot=None if sys.platform == "win32" else 1,
        alias=WORKDIR_ALIAS,
        host_path=args.workdir,
        mode=AgentMode.REVIEW if args.read_only else AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({args.runtime_name}),
        read_only=args.read_only,
    )
    service = BridgeService(
        store=TaskStore(args.state_dir),
        policies=PolicyRegistry([policy]),
        adapters={args.runtime_name: CodexNamedFakeAdapter(args.runtime_name)},
        lease_manager=LeaseManager(args.lock_dir, lease_ids=[policy.lease_id]),
        limits=BridgeLimits(
            task_timeout_seconds=60,
            interaction_timeout_seconds=1,
            # A response over this bound goes to the result spool instead of staying inline, so
            # the driver can exercise the spooled path without a prompt larger than the Agent
            # prompt limit allows.
            max_final_response_bytes=args.max_final_response_bytes,
            result_preview_bytes=min(65_536, args.max_final_response_bytes),
        ),
    )
    await service.start()
    server = BridgeProtocolServer(
        service=service,
        socket_path=args.socket,
        pipe_name=args.pipe_name,
    )
    await server.start()
    print(READY_LINE, flush=True)
    try:
        await server.serve_forever()
    finally:
        await server.close()
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge process for the MCP E2E harness")
    endpoint = parser.add_mutually_exclusive_group(required=True)
    endpoint.add_argument("--socket", type=Path)
    endpoint.add_argument("--pipe-name", help="Windows Named Pipe endpoint, e.g. \\\\.\\pipe\\name")
    parser.add_argument("--lock-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument(
        "--runtime-name",
        default="codex",
        help="public runtime name the FakeAdapter answers to; a raw-RPC driver uses fake",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="serve a review workdir, which needs no writer lease",
    )
    parser.add_argument(
        "--max-final-response-bytes",
        type=int,
        default=262_144,
        help="inline response bound; a longer answer goes to the result spool",
    )
    args = parser.parse_args()
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
