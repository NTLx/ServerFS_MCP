"""Sanitized common projections for native service status mappings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from . import SERVER_VERSION
from .observability import (
    OBSERVATION_SCHEMA_VERSION,
    Applicability,
    ComponentKey,
    ComponentObservation,
    Health,
    ObservationSnapshot,
    PlatformFamily,
    ProcessRole,
    Projection,
    Provenance,
    ReasonCode,
    RolePolicy,
)


def _observation(
    component: ComponentKey,
    health: Health,
    provenance: Provenance,
    reason: ReasonCode,
    now: datetime,
) -> ComponentObservation:
    return ComponentObservation(component, Applicability.ENABLED, health, provenance, reason, now)


def _snapshot(
    platform: PlatformFamily,
    components: tuple[ComponentObservation, ...],
    observed_at: datetime | None,
) -> ObservationSnapshot:
    now = observed_at or datetime.now(UTC)
    return ObservationSnapshot(
        OBSERVATION_SCHEMA_VERSION,
        now,
        SERVER_VERSION,
        ProcessRole.NATIVE_SERVICE,
        platform,
        tuple(
            ComponentObservation(
                item.component, item.applicability, item.health, item.provenance, item.reason, now
            )
            for item in components
        ),
    )


def windows_common_projection(
    native: Mapping[str, object], *, observed_at: datetime | None = None
) -> tuple[ObservationSnapshot, RolePolicy]:
    """Project only fields already returned by ``windows_service.status``."""
    now = observed_at or datetime.now(UTC)
    registered = native.get("registered")
    if registered is False:
        recovery = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.NOT_READY)
    elif registered is None:
        recovery = (Health.UNKNOWN, Provenance.OBSERVED, ReasonCode.PROBE_FAILED)
    elif native.get("manifest_installed") is not True or native.get("task_valid") is False:
        recovery = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.INVALID_CONFIGURATION)
    elif native.get("watchdog_alive") is True and native.get("runner_alive") is True:
        recovery = (Health.HEALTHY, Provenance.OBSERVED, ReasonCode.OK)
    elif native.get("watchdog_alive") is True and native.get("runner_unclean_exit") is True:
        recovery = (Health.DEGRADED, Provenance.OBSERVED, ReasonCode.DEGRADED)
    elif native.get("watchdog_alive") is True:
        recovery = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.NOT_READY)
    else:
        recovery = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.UNAVAILABLE)
    items = [_observation(ComponentKey.RECOVERY, *recovery, now)]

    if native.get("runner_alive") is not True:
        tunnel = (Health.UNKNOWN, Provenance.NOT_CHECKED, ReasonCode.CONFIGURED_NOT_CHECKED)
    else:
        started, exited = native.get("last_tunnel_started_at"), native.get("last_tunnel_exit_at")

        def is_number(value: object) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool)

        if is_number(started) and (not is_number(exited) or started > exited):
            tunnel = (Health.HEALTHY, Provenance.OBSERVED, ReasonCode.OK)
        elif is_number(exited) and (not is_number(started) or exited >= started):
            tunnel = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.UNAVAILABLE)
        else:
            tunnel = (Health.UNKNOWN, Provenance.NOT_CHECKED, ReasonCode.CONFIGURED_NOT_CHECKED)
    items.append(_observation(ComponentKey.TUNNEL, *tunnel, now))
    policy = RolePolicy(
        frozenset({ComponentKey.RECOVERY}),
        frozenset({ComponentKey.RECOVERY, ComponentKey.TUNNEL}),
        frozenset({ComponentKey.RECOVERY, ComponentKey.TUNNEL}),
    )
    return _snapshot(PlatformFamily.WINDOWS, tuple(items), now), policy


def darwin_common_projection(
    native: Mapping[str, object], *, observed_at: datetime | None = None
) -> tuple[ObservationSnapshot, RolePolicy]:
    """Project macOS Tunnel lifecycle evidence, without inferring Bridge applicability."""
    now = observed_at or datetime.now(UTC)
    services = native.get("services")
    services = services if isinstance(services, Mapping) else {}
    processes = native.get("processes")
    processes = processes if isinstance(processes, Mapping) else {}
    tunnel_proc = processes.get("tunnel")
    tunnel_proc = tunnel_proc if isinstance(tunnel_proc, Mapping) else {}
    pid = tunnel_proc.get("pid")
    unmanaged = native.get("unmanaged_tunnel_pids")
    if native.get("bundle_valid") is False:
        result = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.INVALID_CONFIGURATION)
    elif type(pid) is int and pid > 0:
        result = (
            (Health.DEGRADED, Provenance.OBSERVED, ReasonCode.DEGRADED)
            if isinstance(unmanaged, list) and bool(unmanaged)
            else (Health.HEALTHY, Provenance.OBSERVED, ReasonCode.OK)
        )
    else:
        state = services.get("tunnel")
        if state in {"not-installed", "not-registered", "not-found", "requires-approval"}:
            result = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.NOT_READY)
        elif state == "invalid-bundle":
            result = (Health.UNAVAILABLE, Provenance.OBSERVED, ReasonCode.INVALID_CONFIGURATION)
        else:
            result = (Health.UNKNOWN, Provenance.OBSERVED, ReasonCode.UNKNOWN_VALUE)
    item = _observation(ComponentKey.TUNNEL, *result, now)
    required = frozenset({ComponentKey.TUNNEL})
    policy = RolePolicy(required, required, required)
    return _snapshot(PlatformFamily.DARWIN, (item,), now), policy


def common_document(snapshot: ObservationSnapshot, policy: RolePolicy) -> dict[str, object]:
    return {
        "snapshot": snapshot.as_dict(),
        **{
            projection.value: snapshot.aggregate(policy, projection).as_dict()
            for projection in Projection
        },
    }


def render_common_json(snapshot: ObservationSnapshot, policy: RolePolicy) -> str:
    return json.dumps(common_document(snapshot, policy), sort_keys=True) + "\n"


def render_common_human(snapshot: ObservationSnapshot, policy: RolePolicy) -> str:
    lines = [f"role: {snapshot.process_role.value}", f"platform: {snapshot.platform_family.value}"]
    for projection in Projection:
        aggregate = snapshot.aggregate(policy, projection)
        lines.append(f"{projection.value}: {aggregate.health.value} ({aggregate.reason.value})")
    for item in snapshot.components:
        lines.append(
            f"{item.component.value}: {item.applicability.value} {item.health.value} "
            f"{item.provenance.value} ({item.reason.value})"
        )
    return "\n".join(lines) + "\n"
