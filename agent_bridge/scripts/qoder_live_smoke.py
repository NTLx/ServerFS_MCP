"""Live Qoder smoke test for the ServerFS Agent Bridge v0.8.0 runtime.

The production adapter never selects a model. This smoke test is deliberately
different: it pins an explicit disposable test model so validation cannot
silently fall onto a paid model. The default is Qwen3.8-Flash.

Requires:
- an installed/authenticated system qodercli;
- a Bridge config that enables Qoder for the selected workdir.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from serverfs_agent_bridge.adapters import QoderAdapter
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_DEFAULT_SMOKE_MODEL = "Qwen3.8-Flash"


async def _wait_terminal(
    service: BridgeService,
    task_id: str,
    *,
    timeout_seconds: float,
    auto_approve_once: bool = False,
    answer_question: bool = False,
) -> tuple[dict[str, Any], bool, bool]:
    deadline = time.monotonic() + timeout_seconds
    handled_requests: set[str] = set()
    saw_approval = False
    saw_question = False
    while time.monotonic() < deadline:
        task = service.get_task(task_id)
        if task["status"] in _TERMINAL:
            return task, saw_approval, saw_question

        if task["status"] == "waiting_for_approval":
            saw_approval = True
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

        if task["status"] == "waiting_for_question":
            saw_question = True
            pending = task.get("pending_request")
            if not answer_question or not isinstance(pending, dict):
                raise RuntimeError(f"live smoke unexpectedly requires a question: {pending}")
            request_id = pending.get("request_id")
            payload = pending.get("payload")
            if not isinstance(request_id, str) or not isinstance(payload, dict):
                raise RuntimeError(f"invalid question payload from Bridge: {pending}")
            questions = payload.get("questions")
            if (
                not isinstance(questions, list)
                or len(questions) != 1
                or not isinstance(questions[0], dict)
            ):
                raise RuntimeError(f"unexpected Qoder question shape: {pending}")
            question = questions[0]
            question_id = question.get("question_id")
            options = question.get("options")
            if not isinstance(question_id, str) or not isinstance(options, list):
                raise RuntimeError(f"unexpected Qoder question shape: {pending}")
            option_ids = [
                option.get("option_id")
                for option in options
                if isinstance(option, dict) and isinstance(option.get("option_id"), str)
            ]
            if "B" not in option_ids:
                raise RuntimeError(f"Qoder question did not offer expected option B: {pending}")
            if request_id not in handled_requests:
                await service.answer_question(
                    task_id=task_id,
                    request_id=request_id,
                    answers=[
                        {
                            "question_id": question_id,
                            "selected_option_ids": ["B"],
                        }
                    ],
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
    if args.model != _DEFAULT_SMOKE_MODEL:
        raise RuntimeError(
            "Qoder live smoke only permits the explicitly approved test model "
            f"{_DEFAULT_SMOKE_MODEL!r}; got {args.model!r}"
        )

    config = BridgeConfig.load(args.config)
    if not config.qoder.enabled:
        raise RuntimeError("qoder.enabled must be true in the live-smoke config")

    policy = config.policies.get(args.workdir)
    if "qoder" not in policy.runtimes:
        raise RuntimeError(f"workdir {args.workdir!r} does not allow the qoder runtime")
    if policy.read_only or policy.mode.value != "workspace-write":
        raise RuntimeError("Qoder native-mode smoke requires a writable/workspace-write workdir")

    runtime = QoderAdapter(config.qoder, model_override=args.model)
    probe = await runtime.probe()
    if not probe.available:
        raise RuntimeError("Qoder CLI is not available through the configured qoder.qoder_bin")
    print(f"Qoder CLI available: version={probe.version or 'unknown'}")
    print(f"Qoder smoke model: {args.model}")

    temp_root = Path(tempfile.mkdtemp(prefix="serverfs-agent-bridge-qoder-live-"))
    smoke_dir = policy.host_path / f"serverfs-qoder-live-{int(time.time())}"
    smoke_dir.mkdir(mode=0o700)
    try:
        service = BridgeService(
            store=TaskStore(temp_root / "state"),
            policies=config.policies,
            adapters={"qoder": runtime},
            lease_manager=LeaseManager(temp_root / "locks"),
        )
        await service.start()
        try:
            first_marker = "SERVERFS_QODER_BRIDGE_OK"
            first = await service.submit_task(
                runtime="qoder",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Do not use tools. Reply with exactly this token and nothing else: "
                    f"{first_marker}"
                ),
            )
            first_done, first_approval, _ = await _wait_terminal(
                service,
                first["task_id"],
                timeout_seconds=args.timeout,
                auto_approve_once=True,
            )
            _require_success(first_done, first_marker)
            first_record = service.store.get_task(first["task_id"])
            if not first_record.native_session_id:
                raise RuntimeError("Qoder task succeeded without a persisted native session id")
            first_session_id = first_record.native_session_id
            print(f"new session: PASS ({first['task_id']}, approval={first_approval})")

            continuation_marker = "SERVERFS_QODER_CONTINUE_OK"
            second = await service.submit_task(
                runtime="qoder",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Continue this same conversation. Do not use tools. Reply with exactly "
                    f"this token and nothing else: {continuation_marker}"
                ),
                continue_from_task_id=first["task_id"],
            )
            second_done, _, _ = await _wait_terminal(
                service,
                second["task_id"],
                timeout_seconds=args.timeout,
                auto_approve_once=True,
            )
            _require_success(second_done, continuation_marker)
            second_record = service.store.get_task(second["task_id"])
            if second_record.native_session_id != first_session_id:
                raise RuntimeError("Qoder continuation did not preserve the native session id")
            print(f"session continuation: PASS ({second['task_id']})")

            question_marker = "SERVERFS_QODER_QUESTION_OK"
            question = await service.submit_task(
                runtime="qoder",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Use AskUserQuestion exactly once. Ask 'Which option?' with header 'Choice', "
                    "single-select options A and B. Wait for the user's answer. If and only if the "
                    "answer is B, reply exactly "
                    f"{question_marker}. Do not use any other tool."
                ),
            )
            question_done, _, saw_question = await _wait_terminal(
                service,
                question["task_id"],
                timeout_seconds=args.timeout,
                auto_approve_once=True,
                answer_question=True,
            )
            if not saw_question:
                raise RuntimeError(
                    "Qoder completed AskUserQuestion without a Bridge waiting_for_question state"
                )
            _require_success(question_done, question_marker)
            print(f"AskUserQuestion round-trip: PASS ({question['task_id']})")

            write_marker = "SERVERFS_QODER_WRITE_OK"
            target = smoke_dir / "write-check.txt"
            write_task = await service.submit_task(
                runtime="qoder",
                workdir=args.workdir,
                path=smoke_dir.name,
                profile="workspace-write",
                prompt=(
                    "Create write-check.txt in the current working directory. "
                    f"Its exact UTF-8 content must be {write_marker} followed by one newline. "
                    f"After verifying the file, reply exactly {write_marker}."
                ),
            )
            write_done, write_approval, _ = await _wait_terminal(
                service,
                write_task["task_id"],
                timeout_seconds=args.timeout,
                auto_approve_once=True,
            )
            _require_success(write_done, write_marker)
            if not target.is_file():
                raise RuntimeError("Qoder reported success but write-check.txt was not created")
            if target.read_text(encoding="utf-8") != f"{write_marker}\n":
                raise RuntimeError("write-check.txt content does not match expected marker")
            print(f"workspace write: PASS ({write_task['task_id']}, approval={write_approval})")

            print(f"Qoder live smoke: PASS (model={args.model})")
        finally:
            await service.close()
    finally:
        shutil.rmtree(smoke_dir, ignore_errors=True)
        shutil.rmtree(temp_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real Qoder v0.8.0 smoke test")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--model", default=_DEFAULT_SMOKE_MODEL)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
