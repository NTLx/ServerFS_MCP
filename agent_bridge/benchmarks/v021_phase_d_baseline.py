"""Reproducible, local pre-instrumentation baseline for v0.21 Phase D."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import alias_lease_id, slot_lease_id
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


def _percentile(samples: list[float], percentile: float) -> float:
    """Return nearest-rank percentile (rank = ceil(p * n), with one-based ranks)."""
    ordered = sorted(samples)
    rank = max(1, (len(ordered) * percentile + 0.999999999) // 1)
    return ordered[int(rank) - 1]


def _distribution(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("latency sample set must not be empty")
    return {
        "count": len(samples),
        "min_ms": min(samples),
        "median_ms": median(samples),
        "p95_ms": _percentile(samples, 0.95),
        "max_ms": max(samples),
        "mean_ms": mean(samples),
    }


def _rss_kib() -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _make_service(root: Path) -> BridgeService:
    repo = root / "repo"
    repo.mkdir()
    policy = WorkdirAgentPolicy(
        slot=None if sys.platform == "win32" else 1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE,
        runtimes=frozenset({"fake"}),
        read_only=False,
    )
    lease_id = alias_lease_id("repo") if sys.platform == "win32" else slot_lease_id(1)
    return BridgeService(
        store=TaskStore(root / "state"),
        policies=PolicyRegistry([policy]),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(root / "locks", lease_ids=[lease_id]),
    )


async def _wait_terminal(service: BridgeService, task_id: str, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        task = service.get_task(task_id)
        if task["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return task
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"task {task_id} did not reach a terminal state")
        await asyncio.sleep(0)


def _timestamp_delta_ms(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (last - first).total_seconds() * 1000


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    construct_samples: list[float] = []
    start_samples: list[float] = []
    combined_samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v021-startup-") as directory:
        base = Path(directory)
        for index in range(args.startup_iterations):
            root = base / str(index)
            root.mkdir()
            before = time.perf_counter()
            service = _make_service(root)
            constructed = time.perf_counter()
            await service.start()
            started = time.perf_counter()
            construct_samples.append((constructed - before) * 1000)
            start_samples.append((started - constructed) * 1000)
            combined_samples.append((started - before) * 1000)
            await service.close()

    rss_before = _rss_kib()
    with tempfile.TemporaryDirectory(prefix="serverfs-v021-main-") as directory:
        service = _make_service(Path(directory))
        await service.start()
        rss_after_start = _rss_kib()

        idle_wall_start = time.perf_counter()
        idle_cpu_start = time.process_time()
        await asyncio.sleep(args.idle_seconds)
        idle_cpu_seconds = time.process_time() - idle_cpu_start
        idle_wall_seconds = time.perf_counter() - idle_wall_start
        rss_after_idle = _rss_kib()

        submit_samples: list[float] = []
        terminal_samples: list[float] = []
        queue_samples: list[float] = []
        execution_samples: list[float] = []
        for index in range(args.task_iterations):
            external_start = time.perf_counter()
            call_start = time.perf_counter()
            submitted = await service.submit_task(
                runtime="fake",
                workdir="repo",
                path="",
                profile="workspace-write",
                prompt=f"complete:{index}",
            )
            call_end = time.perf_counter()
            task = await _wait_terminal(service, submitted["task_id"], args.task_timeout)
            terminal_end = time.perf_counter()
            if task["status"] != "succeeded":
                raise RuntimeError(f"fake task {submitted['task_id']} ended as {task['status']}")
            submit_samples.append((call_end - call_start) * 1000)
            terminal_samples.append((terminal_end - external_start) * 1000)
            queue = _timestamp_delta_ms(task.get("created_at"), task.get("started_at"))
            execution = _timestamp_delta_ms(task.get("started_at"), task.get("completed_at"))
            if queue is not None:
                queue_samples.append(queue)
            if execution is not None:
                execution_samples.append(execution)
        await service.close()

    lease_success: list[float] = []
    lease_contention: list[float] = []
    with tempfile.TemporaryDirectory(prefix="serverfs-v021-lease-") as directory:
        lease_id = alias_lease_id("repo") if sys.platform == "win32" else slot_lease_id(1)
        manager = LeaseManager(Path(directory) / "locks", lease_ids=[lease_id])
        for _ in range(args.lease_iterations):
            before = time.perf_counter()
            handle = manager.acquire_exclusive(lease_id)
            handle.release()
            lease_success.append((time.perf_counter() - before) * 1000)
        for _ in range(args.contention_iterations):
            held = manager.acquire_exclusive(lease_id)
            before = time.perf_counter()
            try:
                manager.acquire_exclusive(lease_id)
            except BridgeError as exc:
                elapsed = (time.perf_counter() - before) * 1000
                if exc.code != "WORKDIR_BUSY":
                    raise
                lease_contention.append(elapsed)
            else:
                raise RuntimeError("contending lease acquisition unexpectedly succeeded")
            finally:
                held.release()

    return {
        "schema_version": 1,
        "benchmark": "v021_phase_d_baseline",
        "environment": {
            "python_version": platform.python_version(),
            "system": platform.system(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "pid": __import__("os").getpid(),
        },
        "parameters": vars(args),
        "startup": {
            "construct_ms": _distribution(construct_samples),
            "start_ms": _distribution(start_samples),
            "combined_ms": _distribution(combined_samples),
        },
        "idle": {
            "wall_seconds": idle_wall_seconds,
            "cpu_seconds": idle_cpu_seconds,
            "cpu_percent": idle_cpu_seconds / idle_wall_seconds * 100,
            "rss_source": "/proc/self/status VmRSS (KiB) on Linux; unavailable elsewhere",
            "rss_kib": {
                "before_construction": rss_before,
                "after_start": rss_after_start,
                "after_idle": rss_after_idle,
                "delta_after_start": (
                    rss_after_start - rss_before
                    if rss_before is not None and rss_after_start is not None
                    else None
                ),
                "delta_after_idle": (
                    rss_after_idle - rss_after_start
                    if rss_after_idle is not None and rss_after_start is not None
                    else None
                ),
            },
        },
        "tasks": {
            "submit_call_ms": _distribution(submit_samples),
            "submit_to_terminal_ms": _distribution(terminal_samples),
            "persisted_created_to_started_ms": (
                _distribution(queue_samples) if queue_samples else None
            ),
            "persisted_started_to_completed_ms": (
                _distribution(execution_samples) if execution_samples else None
            ),
        },
        "lease": {
            "acquire_release_ms": _distribution(lease_success),
            "contended_acquire_ms": _distribution(lease_contention),
        },
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-iterations", type=_positive_int, default=30)
    parser.add_argument("--task-iterations", type=_positive_int, default=200)
    parser.add_argument("--lease-iterations", type=_positive_int, default=2000)
    parser.add_argument("--contention-iterations", type=_positive_int, default=200)
    parser.add_argument("--idle-seconds", type=_positive_float, default=1.0)
    parser.add_argument("--task-timeout", type=_positive_float, default=30.0)
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
