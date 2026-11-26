"""Content-free stage evidence for macOS Shared Qoder acceptance helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

STAGES = frozenset(
    {
        "fixture_started",
        "owner_started",
        "owner_starting",
        "core_staged",
        "core_ready",
        "task_submitted",
        "task_discovered",
        "task_terminal",
        "foreign_core_denied",
        "foreign_alias_denied",
        "core_restarted",
        "content_hash_verified",
        "teardown_verified",
        "finished",
        "exception",
    }
)
ERROR_CODES = frozenset(
    {
        "AGENT_PROVIDER_ERROR",
        "AGENT_PROVIDER_DISCONNECTED",
        "AGENT_RUNTIME_UNAVAILABLE",
        "AGENT_RUNTIME_NOT_READY",
        "CORE_TASK_NOT_FOUND",
        "CORE_TASK_NOT_AUTHORIZED",
    }
)
ERROR_CLASSES = frozenset(
    {"AssertionError", "BridgeError", "OSError", "TimeoutError", "RuntimeError", "Exception"}
)
SDK_ERROR_CLASSES = frozenset(
    {
        "CancelledError",
        "TimeoutError",
        "OSError",
        "RuntimeError",
        "ConnectionError",
        "FileNotFoundError",
        "PermissionError",
        "Exception",
        "ValueError",
        "TypeError",
        "AttributeError",
        "JSONDecodeError",
        "CLIConnectionError",
        "CLINotFoundError",
        "ProcessError",
        "ModelPolicyTimeoutError",
        "TimeoutErrorCause",
        "CLIConnectionErrorCause",
        "ProcessErrorCause",
    }
)


def safe_sdk_error_class(exc: BaseException) -> str:
    current = exc
    seen = set()
    for _ in range(4):
        if id(current) in seen:
            break
        seen.add(id(current))
        if current is not exc:
            candidate = type(current).__name__ + "Cause"
            if candidate in SDK_ERROR_CLASSES:
                return candidate
        if current.__cause__ is None:
            break
        current = current.__cause__
    name = type(exc).__name__
    return name if name in SDK_ERROR_CLASSES else "OTHER"


class StageEvidence:
    """Write only fixed stage names and allowlisted error metadata."""

    def __init__(
        self,
        path: Path,
        *,
        runtime: str = "qoder",
        source_sha: str | None = None,
        test_file: Path | None = None,
        synthetic: bool = True,
        expected_tasks: int = 1,
    ) -> None:
        if expected_tasks not in (1, 2):
            raise ValueError("unsupported acceptance task count")
        self.expected_tasks = expected_tasks
        self.path = path
        self.records: list[dict[str, str]] = []
        actual_sha = (
            source_sha
            or subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[2],
                text=True,
            ).strip()
        )
        expected_sha = os.environ.get("SERVERFS_ACCEPTANCE_BASE_SHA", actual_sha)
        if not re.fullmatch(r"[0-9a-f]{40}", actual_sha) or expected_sha != actual_sha:
            raise ValueError("acceptance source SHA does not match expected candidate")
        self.metadata = {
            "runtime": runtime,
            "acceptance_base_sha": expected_sha,
            "source_sha": actual_sha,
            "test_file_sha256": hashlib.sha256(test_file.read_bytes()).hexdigest()
            if test_file is not None
            else None,
            "evidence_kind": "synthetic" if synthetic else "real",
            "expected_tasks": expected_tasks,
        }

    def record(
        self,
        stage: str,
        *,
        error_code: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if stage not in STAGES:
            raise ValueError("unsupported diagnostic stage")
        row = {"stage": stage}
        if error_code is not None:
            row["error_code"] = error_code if error_code in ERROR_CODES else "OTHER"
        if error_class is not None:
            row["error_class"] = error_class if error_class in ERROR_CLASSES else "Exception"
        self.records.append(row)
        pending = self.path.with_suffix(".pending")
        with pending.open("w", encoding="utf-8") as stream:
            os.chmod(pending, 0o600)
            json.dump({**self.metadata, "stages": self.records}, stream, sort_keys=True)
            stream.write("\n")
        pending.replace(self.path)

    def diagnose(self) -> dict[str, str]:
        has_failure = any("error_class" in row for row in self.records)
        completed = [
            row["stage"]
            for row in self.records
            if "error_class" not in row
            and (not has_failure or row["stage"] not in {"teardown_verified", "finished"})
        ]
        expected = (
            (
                "fixture_started",
                "owner_starting",
                "owner_started",
                "core_staged",
                "core_ready",
            )
            + ("task_submitted", "task_discovered", "task_terminal") * self.expected_tasks
            + (
                "foreign_core_denied",
                "foreign_alias_denied",
                "core_restarted",
                "content_hash_verified",
                "teardown_verified",
                "finished",
            )
        )
        index = 0
        for stage in completed:
            if index < len(expected) and stage == expected[index]:
                index += 1
        return {
            "last_completed_stage": expected[index - 1] if index else "none",
            "next_expected_stage": expected[index] if index < len(expected) else "none",
        }
