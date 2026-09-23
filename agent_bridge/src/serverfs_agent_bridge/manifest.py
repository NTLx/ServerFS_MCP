"""Immutable execution manifest helpers for Agent Bridge tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import __version__
from .models import RuntimeInfo
from .policy import WorkdirAgentPolicy

MANIFEST_SCHEMA_VERSION = 1
BRIDGE_PROTOCOL_VERSION = 1


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy_fingerprint(policy: WorkdirAgentPolicy) -> str:
    canonical = canonical_json(
        {
            "alias": policy.alias,
            "slot": policy.slot,
            "read_only": policy.read_only,
            "agent_mode": policy.mode.value,
            "agent_runtimes": sorted(policy.runtimes),
        }
    )
    return sha256_text(canonical)


def build_manifest(
    *,
    runtime_info: RuntimeInfo,
    policy: WorkdirAgentPolicy,
    relative_cwd: str,
    profile: str,
    limits: dict[str, int],
    advisor: dict[str, Any],
    continue_from_task_id: str | None,
    correlation_id: str | None,
    deadline_at: str,
) -> tuple[dict[str, Any], str, str]:
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bridge_version": __version__,
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "runtime": {
            "name": runtime_info.name,
            "version": runtime_info.version,
            "in_flight_recovery": runtime_info.in_flight_recovery,
        },
        "workspace": {
            "alias": policy.alias,
            "slot": policy.slot,
            "relative_cwd": relative_cwd,
            "profile": profile,
        },
        "policy": {
            "read_only": policy.read_only,
            "agent_mode": policy.mode.value,
            "policy_sha256": policy_fingerprint(policy),
        },
        "limits": limits,
        "advisor": advisor,
        "continuation": {"from_task_id": continue_from_task_id},
        "correlation_id": correlation_id,
        "deadline_at": deadline_at,
    }
    encoded = canonical_json(manifest)
    return manifest, encoded, sha256_text(encoded)
