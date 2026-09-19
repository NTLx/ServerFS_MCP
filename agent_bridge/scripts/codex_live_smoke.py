"""Live Codex daemon smoke test for ServerFS Agent Bridge Phase B.

This script is intentionally excluded from the normal pytest suite.  It requires:
- an installed/authenticated Codex CLI;
- the official managed app-server daemon (or codex.autostart=true);
- a Bridge config that enables Codex for the selected workdir.

It never changes the production ServerFS MCP or Compose deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from serverfs_agent_bridge.adapters import CodexAdapter
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_WAITING = {"waiting_for_approval", "waiting_for_question"}


async def _wait_terminal(
    service: BridgeService,
    task_id: str,
    *,
    timeout_seconds: float,
    auto_approve_once: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    handled_requests: set[str] = set()
    while time.monotonic() < deadline:
        task = service.get_task(task_id)
        if task["status"] in _TERMINAL:
            return task
        if task["status"] == "waiting_for_question":
            raise RuntimeError(
                f"live smoke unexpectedly requires a user answer: {task['pending_request']}"
            )
        if task["status"] == "waiting_for_approval":
            pending = task.get("pending_request")
            if not auto_approve_once or not isinstance(pending, dict):
                raise RuntimeError(f"live smoke unexpectedly requires approval: {pending}")
            request_id = pending.get("request_id")
            payload = pending.get("payload")
            if not isinstance(request_id, str) or not isinstance(payload, dict):
                raise RuntimeError(f"invalid approval payload from Bridge: {pending}")
            if request_id not in handled_requests:
                decisions = payload.get("available_decisions", [])
                if "approve_once" not in decisions:
                    raise RuntimeError(
                        f"live smoke approval does not offer approve_once: {pending}"
                    )
                await service.respond_approval(
                    task_id=task_id,
                    request_id=request_id,
                    decision="approve_once",
                )
                handled_requests.add(request_id)
            await asyncio.sleep(0)
            continue
        await asyncio.sleep(0.25)
    await service.cancel_task(task_id)
    raise TimeoutError(f"task {task_id} did not finish within {timeout_seconds}s")


def _require_success(task: dict[str, Any], marker: str) -> None:
    if task["status"] != "succeeded":
        raise RuntimeError(
            f"task {task['task_id']} ended as {task['status']}: "
            f"{task.get('error_code')} {task.get('error_message')}"
        )
    response = task.get("final_response")
    if not isinstance(response, str) or marker not in response:
        raise RuntimeError(
            f"task {task['task_id']} did not return expected marker {marker!r}: {response!r}"
        )


async def _run(args: argparse.Namespace) -> None:
    config = BridgeConfig.load(args.config)
    if not config.codex.enabled:
        raise RuntimeError("codex.enabled must be true in the live-smoke config")

    policy = config.policies.get(args.workdir)
    if "codex" not in policy.runtimes:
        raise RuntimeError(f"workdir {args.workdir!r} does not allow the codex runtime")
    if policy.read_only or policy.mode.value != "workspace-write":
        raise RuntimeError("Codex native-mode smoke requires a writable/workspace-write workdir")

    runtime = CodexAdapter(config.codex)
    probe = await runtime.probe()
    if not probe.available:
        raise RuntimeError(
            "Codex daemon is not available. Start it with the official "
            "codex app-server daemon start command or enable codex.autostart."
        )
    print(f"Codex daemon available: version={probe.version or 'unknown'}")

    temp_root = Path(tempfile.mkdtemp(prefix="serverfs-agent-bridge-live-"))
    smoke_dir = policy.host_path / f"serverfs-codex-live-{int(time.time())}"
    smoke_dir.mkdir(mode=0o700)
    try:
        service = BridgeService(
            store=TaskStore(temp_root / "state"),
            policies=config.policies,
            adapters={"codex": runtime},
            lease_manager=LeaseManager(temp_root / "locks"),
        )
        await service.start()
        try:
            first_marker = "SERVERFS_CODEX_BRIDGE_OK"
            first = await service.submit_task(
                runtime="codex",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Do not use tools. Reply with exactly this token and nothing else: "
                    f"{first_marker}"
                ),
            )
            first_done = await _wait_terminal(
                service, first["task_id"], timeout_seconds=args.timeout
            )
            _require_success(first_done, first_marker)
            print(f"new thread: PASS ({first['task_id']})")

            continuation_marker = "SERVERFS_CODEX_CONTINUE_OK"
            second = await service.submit_task(
                runtime="codex",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Continue this same conversation. Do not use tools. Reply with exactly "
                    f"this token and nothing else: {continuation_marker}"
                ),
                continue_from_task_id=first["task_id"],
            )
            second_done = await _wait_terminal(
                service, second["task_id"], timeout_seconds=args.timeout
            )
            _require_success(second_done, continuation_marker)
            print(f"thread continuation: PASS ({second['task_id']})")

            write_marker = "SERVERFS_CODEX_WRITE_OK"
            target = smoke_dir / "write-check.txt"
            third = await service.submit_task(
                runtime="codex",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Create the file write-check.txt in the current working directory. "
                    f"Its exact UTF-8 content must be {write_marker} followed by one newline. "
                    f"After verifying the file, reply exactly {write_marker}."
                ),
            )
            third_done = await _wait_terminal(
                service,
                third["task_id"],
                timeout_seconds=args.timeout,
                auto_approve_once=True,
            )
            _require_success(third_done, write_marker)
            if not target.is_file():
                raise RuntimeError("Codex reported success but write-check.txt was not created")
            if target.read_text(encoding="utf-8") != f"{write_marker}\n":
                raise RuntimeError("write-check.txt content does not match expected marker")
            print(f"workspace write: PASS ({third['task_id']})")

            print("Codex live smoke: PASS")
        finally:
            await service.close()
    finally:
        shutil.rmtree(smoke_dir, ignore_errors=True)
        shutil.rmtree(temp_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real Codex daemon Phase B smoke test")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
