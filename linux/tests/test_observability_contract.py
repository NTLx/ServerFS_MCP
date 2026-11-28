from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from serverfs_mcp.observability import (
    MAX_METRIC_SERIES,
    METRIC_CATALOG,
    OBSERVATION_SCHEMA_VERSION,
    TOTAL_METRIC_SERIES_CEILING,
    AggregateResult,
    Applicability,
    ComponentKey,
    ComponentObservation,
    DimensionSpec,
    Health,
    ObservationSnapshot,
    PlatformFamily,
    ProcessRole,
    Projection,
    Provenance,
    ReasonCode,
    RolePolicy,
    validate_metric_labels,
)

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)


def _component(
    component: ComponentKey,
    *,
    health: Health = Health.HEALTHY,
    provenance: Provenance = Provenance.OBSERVED,
    applicability: Applicability = Applicability.ENABLED,
    reason: ReasonCode = ReasonCode.OK,
    observed_at: datetime = NOW,
) -> ComponentObservation:
    return ComponentObservation(
        component=component,
        applicability=applicability,
        health=health,
        provenance=provenance,
        reason=reason,
        observed_at=observed_at,
    )


def _snapshot(*components: ComponentObservation) -> ObservationSnapshot:
    return ObservationSnapshot(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        observed_at=NOW,
        product_version="0.21.0",
        process_role=ProcessRole.MCP,
        platform_family=PlatformFamily.LINUX,
        components=tuple(components),
    )


def _policy() -> RolePolicy:
    return RolePolicy(
        liveness_required=frozenset({ComponentKey.MCP_TRANSPORT}),
        readiness_required=frozenset({ComponentKey.MCP_TRANSPORT, ComponentKey.WORKDIRS}),
        health_required=frozenset({ComponentKey.MCP_TRANSPORT, ComponentKey.WORKDIRS}),
        health_optional=frozenset({ComponentKey.TUNNEL, ComponentKey.AGENT_BRIDGE}),
    )


def test_healthy_requires_direct_observation() -> None:
    with pytest.raises(ValueError, match="healthy requires"):
        _component(ComponentKey.MCP_TRANSPORT, provenance=Provenance.CONFIGURED)


def test_configured_and_not_checked_cannot_assert_health() -> None:
    for provenance in (Provenance.CONFIGURED, Provenance.NOT_CHECKED):
        with pytest.raises(ValueError, match="cannot assert health"):
            _component(
                ComponentKey.AUTHENTICATION,
                health=Health.DEGRADED,
                provenance=provenance,
                reason=ReasonCode.CONFIGURED_NOT_CHECKED,
            )


def test_stale_evidence_cannot_assert_current_health() -> None:
    with pytest.raises(ValueError, match="stale evidence"):
        _component(
            ComponentKey.TUNNEL,
            health=Health.HEALTHY,
            provenance=Provenance.STALE,
            reason=ReasonCode.STALE,
        )


def test_non_enabled_component_has_explicit_unknown_semantics() -> None:
    disabled = _component(
        ComponentKey.AGENT_BRIDGE,
        health=Health.UNKNOWN,
        provenance=Provenance.CONFIGURED,
        applicability=Applicability.DISABLED,
        reason=ReasonCode.DISABLED,
    )
    assert disabled.health is Health.UNKNOWN
    assert disabled.reason is ReasonCode.DISABLED

    with pytest.raises(ValueError, match="applicability and reason"):
        _component(
            ComponentKey.AGENT_BRIDGE,
            health=Health.UNKNOWN,
            provenance=Provenance.CONFIGURED,
            applicability=Applicability.DISABLED,
            reason=ReasonCode.OK,
        )


def test_observation_timestamps_are_aware_and_not_from_future() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _component(ComponentKey.MCP_TRANSPORT, observed_at=datetime(2026, 10, 22))

    future = _component(ComponentKey.MCP_TRANSPORT, observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="newer than snapshot"):
        _snapshot(future)


def test_snapshot_rejects_duplicate_components() -> None:
    one = _component(ComponentKey.MCP_TRANSPORT)
    with pytest.raises(ValueError, match="duplicate component"):
        _snapshot(one, one)


def test_liveness_readiness_and_health_are_distinct_projections() -> None:
    snapshot = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(ComponentKey.WORKDIRS),
        _component(
            ComponentKey.TUNNEL,
            health=Health.UNAVAILABLE,
            reason=ReasonCode.UNAVAILABLE,
        ),
    )
    policy = _policy()

    assert snapshot.aggregate(policy, Projection.LIVENESS) == AggregateResult(
        Health.HEALTHY, ReasonCode.ALL_REQUIRED_HEALTHY
    )
    assert snapshot.aggregate(policy, Projection.READINESS) == AggregateResult(
        Health.HEALTHY, ReasonCode.ALL_REQUIRED_HEALTHY
    )
    assert snapshot.aggregate(policy, Projection.HEALTH) == AggregateResult(
        Health.DEGRADED, ReasonCode.OPTIONAL_COMPONENT_DEGRADED
    )


def test_required_unavailable_degraded_unknown_and_missing_precedence() -> None:
    policy = _policy()
    unavailable = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(
            ComponentKey.WORKDIRS,
            health=Health.UNAVAILABLE,
            reason=ReasonCode.UNAVAILABLE,
        ),
    )
    assert unavailable.aggregate(policy, Projection.READINESS) == AggregateResult(
        Health.UNAVAILABLE, ReasonCode.REQUIRED_COMPONENT_UNAVAILABLE
    )

    degraded = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(
            ComponentKey.WORKDIRS,
            health=Health.DEGRADED,
            reason=ReasonCode.DEGRADED,
        ),
    )
    assert degraded.aggregate(policy, Projection.READINESS) == AggregateResult(
        Health.DEGRADED, ReasonCode.REQUIRED_COMPONENT_DEGRADED
    )

    unknown = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(
            ComponentKey.WORKDIRS,
            health=Health.UNKNOWN,
            provenance=Provenance.NOT_CHECKED,
            reason=ReasonCode.CONFIGURED_NOT_CHECKED,
        ),
    )
    assert unknown.aggregate(policy, Projection.READINESS) == AggregateResult(
        Health.UNKNOWN, ReasonCode.REQUIRED_COMPONENT_UNKNOWN
    )

    missing = _snapshot(_component(ComponentKey.MCP_TRANSPORT))
    assert missing.aggregate(policy, Projection.READINESS) == AggregateResult(
        Health.UNKNOWN, ReasonCode.REQUIRED_COMPONENT_MISSING
    )


def test_disabled_optional_component_does_not_degrade_health() -> None:
    snapshot = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(ComponentKey.WORKDIRS),
        _component(
            ComponentKey.TUNNEL,
            applicability=Applicability.DISABLED,
            health=Health.UNKNOWN,
            provenance=Provenance.CONFIGURED,
            reason=ReasonCode.DISABLED,
        ),
    )
    assert snapshot.aggregate(_policy(), Projection.HEALTH) == AggregateResult(
        Health.HEALTHY, ReasonCode.ALL_REQUIRED_HEALTHY
    )


def test_serialization_is_bounded_and_round_trips_without_sensitive_fields() -> None:
    snapshot = _snapshot(
        _component(ComponentKey.MCP_TRANSPORT),
        _component(
            ComponentKey.AUTHENTICATION,
            health=Health.UNKNOWN,
            provenance=Provenance.CONFIGURED,
            reason=ReasonCode.CONFIGURED_NOT_CHECKED,
        ),
    )
    document = snapshot.as_dict()
    flattened = repr(document).lower()
    for forbidden in (
        "hostname",
        "username",
        "task_id",
        "workdir",
        "path",
        "model",
        "session",
        "prompt",
        "exception",
    ):
        assert forbidden not in flattened
    assert ObservationSnapshot.from_dict(document) == snapshot


def test_future_unknown_component_is_rejected_but_unknown_state_values_fail_closed() -> None:
    base = _component(ComponentKey.TUNNEL).as_dict()

    future_component = {**base, "component": "future_component"}
    with pytest.raises(ValueError, match="unknown component"):
        ComponentObservation.from_dict(future_component)

    future_health = {**base, "health": "super_healthy"}
    parsed = ComponentObservation.from_dict(future_health)
    assert parsed.health is Health.UNKNOWN
    assert parsed.provenance is Provenance.NOT_CHECKED
    assert parsed.reason is ReasonCode.UNKNOWN_VALUE

    future_applicability = {**base, "applicability": "future_mode"}
    parsed = ComponentObservation.from_dict(future_applicability)
    assert parsed.applicability is Applicability.UNSUPPORTED
    assert parsed.health is Health.UNKNOWN
    assert parsed.reason is ReasonCode.UNSUPPORTED


def test_unknown_reason_cannot_preserve_a_healthy_claim() -> None:
    future_reason = {**_component(ComponentKey.MCP_TRANSPORT).as_dict(), "reason": "future_reason"}
    parsed = ComponentObservation.from_dict(future_reason)
    assert parsed.health is Health.UNKNOWN
    assert parsed.provenance is Provenance.NOT_CHECKED
    assert parsed.reason is ReasonCode.UNKNOWN_VALUE


def test_snapshot_parser_rejects_unknown_identity_and_schema() -> None:
    document = _snapshot(_component(ComponentKey.MCP_TRANSPORT)).as_dict()
    with pytest.raises(ValueError, match="schema"):
        ObservationSnapshot.from_dict({**document, "schema_version": 999})
    with pytest.raises(ValueError, match="identity"):
        ObservationSnapshot.from_dict({**document, "platform_family": "future_os"})


def test_metric_catalog_has_finite_bounded_cardinality() -> None:
    assert METRIC_CATALOG
    assert TOTAL_METRIC_SERIES_CEILING <= MAX_METRIC_SERIES
    assert len({definition.name for definition in METRIC_CATALOG}) == len(METRIC_CATALOG)
    assert all(definition.series_ceiling > 0 for definition in METRIC_CATALOG)


def test_metric_dimension_names_exclude_sensitive_identity_classes() -> None:
    dimension_names = {
        dimension.name for definition in METRIC_CATALOG for dimension in definition.dimensions
    }
    forbidden = {
        "task_id",
        "workdir",
        "path",
        "user",
        "principal",
        "model",
        "session",
        "correlation_id",
        "idempotency_key",
        "hostname",
        "ip",
        "url",
        "file",
        "prompt",
        "error",
        "exception",
    }
    assert dimension_names.isdisjoint(forbidden)
    with pytest.raises(ValueError, match="sensitive"):
        DimensionSpec("task_id", frozenset({"one"}))


def test_metric_labels_reject_arbitrary_or_high_cardinality_values() -> None:
    validate_metric_labels(
        "serverfs_agent_tasks_total",
        {"outcome": "succeeded", "runtime_family": "acp", "mode": "workspace-write"},
    )
    with pytest.raises(ValueError, match="outside its bounded domain"):
        validate_metric_labels(
            "serverfs_agent_tasks_total",
            {
                "outcome": "succeeded",
                "runtime_family": "acp-user-controlled-profile-name",
                "mode": "workspace-write",
            },
        )
    with pytest.raises(ValueError, match="do not match"):
        validate_metric_labels(
            "serverfs_agent_tasks_total",
            {
                "outcome": "succeeded",
                "runtime_family": "acp",
                "mode": "workspace-write",
                "task_id": "agt_secret",
            },
        )
