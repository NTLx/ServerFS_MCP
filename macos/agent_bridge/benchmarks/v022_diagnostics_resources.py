"""Measure process-local diagnostic retention, idle RSS, and side-effect bounds on Linux."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import platform
import tempfile
import threading
import time
from pathlib import Path

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.diagnostics import (
    COMPLETENESS,
    INTERACTION_CLASSES,
    LEASE_OUTCOMES,
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_RECORDS,
    MAX_TASK_DIAGNOSTIC_RECORDS,
    OPERATION_KINDS,
    DiagnosticOperation,
    DiagnosticSeal,
)
from serverfs_agent_bridge.lease_identity import slot_lease_id
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.observability import (
    OUTCOMES,
    REASON_CODES,
    RECOVERY_OUTCOMES,
    RUNTIME_FAMILIES,
)
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore


def rss_kib() -> int:
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS unavailable")


def file_state(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def maximum_legal_payload_bytes() -> tuple[int, int, dict[int, DiagnosticOperation]]:
    """Exhaust the closed operation catalog and include the largest finite JSON numbers."""
    maximum_operation = 0
    maximum_operations: dict[int, DiagnosticOperation] = {}
    for kind, runtime, reason, interaction, outcome, sequence in itertools.product(
        sorted(OPERATION_KINDS),
        [None, *sorted(RUNTIME_FAMILIES)],
        [None, *sorted(REASON_CODES)],
        [None, *sorted(INTERACTION_CLASSES)],
        [None, *sorted(OUTCOMES | LEASE_OUTCOMES | RECOVERY_OUTCOMES | {"started"})],
        range(MAX_TASK_DIAGNOSTIC_RECORDS - 1),
    ):
        if (kind == "agent.waiting_external") != (interaction is not None):
            continue
        try:
            operation = DiagnosticOperation(
                kind,
                1e308,
                sequence,
                outcome=outcome,
                reason_code=reason,
                runtime_family=runtime,
                interaction_class=interaction,
            )
        except ValueError:
            continue
        encoded = json.dumps(
            operation.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        maximum_operation = max(maximum_operation, len(encoded))
        if sequence not in maximum_operations or len(encoded) > len(
            json.dumps(maximum_operations[sequence].to_dict(), separators=(",", ":")).encode()
        ):
            maximum_operations[sequence] = operation
    maximum_seal = max(
        len(
            json.dumps(
                DiagnosticSeal(outcome, reason, 1e308, 1e308, completeness).to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        )
        for outcome, reason in (
            ("succeeded", "ok"),
            ("failed", "degraded"),
            ("cancelled", "safe"),
            ("interrupted", "unavailable"),
        )
        for completeness in COMPLETENESS
    )
    return maximum_operation, maximum_seal, maximum_operations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    if not platform.system() == "Linux":
        raise SystemExit("this acceptance measurement requires Linux /proc")

    with tempfile.TemporaryDirectory(prefix="serverfs-v022-resources-") as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        lease_id = slot_lease_id(1)
        service = BridgeService(
            store=TaskStore(root / "state"),
            policies=PolicyRegistry(
                [
                    WorkdirAgentPolicy(
                        slot=1,
                        alias="repo",
                        host_path=repo,
                        mode=AgentMode.WORKSPACE_WRITE,
                        runtimes=frozenset({"fake"}),
                        read_only=False,
                    )
                ]
            ),
            adapters={"fake": FakeAdapter()},
            lease_manager=LeaseManager(root / "locks", lease_ids=[lease_id]),
        )
        asyncio.run(service.start())
        recorder = service.diagnostic_recorder
        files_before = file_state(root)
        baseline = rss_kib()
        thread_count_before = threading.active_count()
        maximum_operation_bytes, maximum_seal_bytes, maximum_operations = (
            maximum_legal_payload_bytes()
        )
        task_count = MAX_DIAGNOSTIC_RECORDS // MAX_TASK_DIAGNOSTIC_RECORDS
        for index in range(task_count):
            task_id = f"resource-{index}"
            assert recorder.admit_task(task_id, "acp")
            for sequence in range(MAX_TASK_DIAGNOSTIC_RECORDS - 1):
                assert recorder.record(task_id, maximum_operations[sequence])
        for index in range(task_count):
            assert recorder.seal_task(f"resource-{index}", "succeeded", completed_at=float(index))
        retained = rss_kib()
        rss_samples = []
        for _ in range(args.idle_samples):
            time.sleep(args.interval)
            rss_samples.append(rss_kib())
        thread_count_after = threading.active_count()
        files_after = file_state(root)
        created_files = sorted(set(files_after) - set(files_before))
        before_sqlite_bytes = sum(
            size
            for name, size in files_before.items()
            if name.endswith((".db", ".sqlite", ".sqlite3"))
        )
        after_sqlite_bytes = sum(
            size
            for name, size in files_after.items()
            if name.endswith((".db", ".sqlite", ".sqlite3"))
        )
        asyncio.run(service.close())

    theoretical_record_payload_upper_bound = MAX_DIAGNOSTIC_RECORDS * max(
        maximum_operation_bytes, maximum_seal_bytes
    )
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pid": os.getpid(),
        },
        "limits": {
            "global_logical_payload_ceiling_bytes": MAX_DIAGNOSTIC_BYTES,
            "global_record_ceiling": MAX_DIAGNOSTIC_RECORDS,
            "maximum_legal_operation_payload_bytes": maximum_operation_bytes,
            "maximum_terminal_seal_payload_bytes": maximum_seal_bytes,
            "record_count_derived_payload_upper_bound_bytes": (
                theoretical_record_payload_upper_bound
            ),
            "logical_byte_ceiling_reachable": (
                theoretical_record_payload_upper_bound >= MAX_DIAGNOSTIC_BYTES
            ),
        },
        "retained": {
            "records": recorder.retained_record_count,
            "logical_payload_bytes": recorder.retained_bytes,
            "logical_byte_ceiling_utilization": recorder.retained_bytes / MAX_DIAGNOSTIC_BYTES,
            "task_slots": len(recorder._slots),
            "rss_baseline_kib": baseline,
            "rss_after_retention_kib": retained,
            "rss_increment_kib": retained - baseline,
            "idle_rss_kib": rss_samples,
            "idle_rss_range_kib": max(rss_samples) - min(rss_samples),
        },
        "side_effects": {
            "threads_before": thread_count_before,
            "threads_after": thread_count_after,
            "files_created": created_files,
            "sqlite_bytes_before": before_sqlite_bytes,
            "sqlite_bytes_after": after_sqlite_bytes,
            "sqlite_growth_bytes": after_sqlite_bytes - before_sqlite_bytes,
            "files_before": files_before,
            "files_after": files_after,
        },
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
