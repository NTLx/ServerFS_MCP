"""Internal, secret-safe operational observation contract for ServerFS.

This module deliberately contains no collectors, network listeners, exporters, filesystem probes,
or provider integrations.  It normalizes already-authorized local facts into a bounded vocabulary so
operator surfaces can agree without turning observability into another authority channel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import prod

OBSERVATION_SCHEMA_VERSION = 1
MAX_METRIC_SERIES = 10_000


class PlatformFamily(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"
    DARWIN = "darwin"


class ProcessRole(StrEnum):
    MCP = "mcp"
    NATIVE_SERVICE = "native_service"
    AGENT_BRIDGE = "agent_bridge"
    TUNNEL_SUMMARY = "tunnel_summary"


class ComponentKey(StrEnum):
    MCP_TRANSPORT = "mcp_transport"
    AUTHENTICATION = "authentication"
    WORKDIRS = "workdirs"
    TUNNEL = "tunnel"
    FILE_INGRESS = "file_ingress"
    OAUTH_BROKER = "oauth_broker"
    AGENT_BRIDGE = "agent_bridge"
    AGENT_RUNTIME = "agent_runtime"
    AGENT_TASKS = "agent_tasks"
    WRITER_LEASE = "writer_lease"
    RECOVERY = "recovery"


class Applicability(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


class Health(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    OBSERVED = "observed"
    CONFIGURED = "configured"
    NOT_CHECKED = "not_checked"
    STALE = "stale"


class Projection(StrEnum):
    LIVENESS = "liveness"
    READINESS = "readiness"
    HEALTH = "health"


class ReasonCode(StrEnum):
    OK = "ok"
    SAFE = "safe"
    ABSENT = "absent"
    DISABLED = "disabled"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"
    CONFIGURED_NOT_CHECKED = "configured_not_checked"
    STALE = "stale"
    PROBE_FAILED = "probe_failed"
    INVALID_CONFIGURATION = "invalid_configuration"
    NOT_READY = "not_ready"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    UNSAFE = "unsafe"
    BUSY = "busy"
    RECOVERY_GUARD = "recovery_guard"
    STOP_UNPROVEN = "stop_unproven"
    REQUIRED_COMPONENT_MISSING = "required_component_missing"
    REQUIRED_COMPONENT_UNAVAILABLE = "required_component_unavailable"
    REQUIRED_COMPONENT_DEGRADED = "required_component_degraded"
    REQUIRED_COMPONENT_UNKNOWN = "required_component_unknown"
    OPTIONAL_COMPONENT_DEGRADED = "optional_component_degraded"
    OPTIONAL_COMPONENT_UNKNOWN = "optional_component_unknown"
    ALL_REQUIRED_HEALTHY = "all_required_healthy"
    UNKNOWN_VALUE = "unknown_value"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("observation timestamp must be a non-empty ISO-8601 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("invalid observation timestamp") from exc
    return _aware_utc(parsed)


def _timestamp_text(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _enum_or(value: object, enum_type: type[StrEnum], fallback: StrEnum) -> StrEnum:
    if not isinstance(value, str):
        return fallback
    try:
        return enum_type(value)
    except ValueError:
        return fallback


@dataclass(frozen=True)
class ComponentObservation:
    """One bounded observation; there is intentionally no free-form detail field."""

    component: ComponentKey
    applicability: Applicability
    health: Health
    provenance: Provenance
    reason: ReasonCode
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at))
        if self.applicability is not Applicability.ENABLED:
            if self.health is not Health.UNKNOWN:
                raise ValueError("disabled/not-applicable/unsupported components must be unknown")
            expected = {
                Applicability.DISABLED: ReasonCode.DISABLED,
                Applicability.NOT_APPLICABLE: ReasonCode.NOT_APPLICABLE,
                Applicability.UNSUPPORTED: ReasonCode.UNSUPPORTED,
            }[self.applicability]
            if self.reason is not expected:
                raise ValueError("component applicability and reason code disagree")
        if self.provenance is Provenance.STALE and self.health is Health.HEALTHY:
            raise ValueError("stale evidence cannot assert current health")
        if self.health is Health.HEALTHY and self.provenance is not Provenance.OBSERVED:
            raise ValueError("healthy requires directly observed evidence")
        if self.provenance in {Provenance.CONFIGURED, Provenance.NOT_CHECKED}:
            if self.health is not Health.UNKNOWN:
                raise ValueError("configured/not-checked evidence cannot assert health")

    def age_seconds(self, *, now: datetime) -> float:
        age = (_aware_utc(now) - self.observed_at).total_seconds()
        return max(0.0, age)

    def as_dict(self) -> dict[str, str]:
        return {
            "component": self.component.value,
            "applicability": self.applicability.value,
            "health": self.health.value,
            "provenance": self.provenance.value,
            "reason": self.reason.value,
            "observed_at": _timestamp_text(self.observed_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ComponentObservation:
        component_value = value.get("component")
        if not isinstance(component_value, str):
            raise ValueError("component must be a string")
        try:
            component = ComponentKey(component_value)
        except ValueError as exc:
            # A future/foreign component cannot safely inherit an existing component's policy.
            raise ValueError("unknown component") from exc

        applicability = _enum_or(
            value.get("applicability"), Applicability, Applicability.UNSUPPORTED
        )
        assert isinstance(applicability, Applicability)
        health = _enum_or(value.get("health"), Health, Health.UNKNOWN)
        assert isinstance(health, Health)
        provenance = _enum_or(value.get("provenance"), Provenance, Provenance.NOT_CHECKED)
        assert isinstance(provenance, Provenance)
        reason = _enum_or(value.get("reason"), ReasonCode, ReasonCode.UNKNOWN_VALUE)
        assert isinstance(reason, ReasonCode)

        # Unknown future enum values fail closed. Applicability has the strongest semantic
        # consequence, so an unknown applicability is normalized into an explicit unsupported
        # component with no health claim.
        raw_applicability = value.get("applicability")
        if not isinstance(raw_applicability, str) or raw_applicability not in {
            item.value for item in Applicability
        }:
            applicability = Applicability.UNSUPPORTED
            health = Health.UNKNOWN
            provenance = Provenance.NOT_CHECKED
            reason = ReasonCode.UNSUPPORTED
        elif not isinstance(value.get("health"), str) or value.get("health") not in {
            item.value for item in Health
        }:
            health = Health.UNKNOWN
            provenance = Provenance.NOT_CHECKED
            reason = ReasonCode.UNKNOWN_VALUE
        elif not isinstance(value.get("provenance"), str) or value.get("provenance") not in {
            item.value for item in Provenance
        }:
            health = Health.UNKNOWN
            provenance = Provenance.NOT_CHECKED
            reason = ReasonCode.UNKNOWN_VALUE
        elif not isinstance(value.get("reason"), str) or value.get("reason") not in {
            item.value for item in ReasonCode
        }:
            # Unknown reason text is never retained.
            reason = ReasonCode.UNKNOWN_VALUE
            if health is Health.HEALTHY:
                health = Health.UNKNOWN
                provenance = Provenance.NOT_CHECKED

        return cls(
            component=component,
            applicability=applicability,
            health=health,
            provenance=provenance,
            reason=reason,
            observed_at=_parse_timestamp(value.get("observed_at")),
        )


@dataclass(frozen=True)
class AggregateResult:
    health: Health
    reason: ReasonCode

    def as_dict(self) -> dict[str, str]:
        return {"health": self.health.value, "reason": self.reason.value}


@dataclass(frozen=True)
class RolePolicy:
    """Components required by each operational projection for one declared role."""

    liveness_required: frozenset[ComponentKey]
    readiness_required: frozenset[ComponentKey]
    health_required: frozenset[ComponentKey]
    health_optional: frozenset[ComponentKey] = frozenset()

    def required(self, projection: Projection) -> frozenset[ComponentKey]:
        if projection is Projection.LIVENESS:
            return self.liveness_required
        if projection is Projection.READINESS:
            return self.readiness_required
        return self.health_required


@dataclass(frozen=True)
class ObservationSnapshot:
    schema_version: int
    observed_at: datetime
    product_version: str
    process_role: ProcessRole
    platform_family: PlatformFamily
    components: tuple[ComponentObservation, ...]

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError("unsupported observation schema version")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at))
        if not self.product_version or len(self.product_version) > 64:
            raise ValueError("invalid product version")
        keys = [item.component for item in self.components]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate component observation")
        if any(item.observed_at > self.observed_at for item in self.components):
            raise ValueError("component observation cannot be newer than snapshot")

    def component_map(self) -> dict[ComponentKey, ComponentObservation]:
        return {item.component: item for item in self.components}

    def aggregate(self, policy: RolePolicy, projection: Projection) -> AggregateResult:
        by_component = self.component_map()
        required = policy.required(projection)
        missing = required.difference(by_component)
        if missing:
            return AggregateResult(Health.UNKNOWN, ReasonCode.REQUIRED_COMPONENT_MISSING)

        required_items = [by_component[key] for key in required]
        if any(item.health is Health.UNAVAILABLE for item in required_items):
            return AggregateResult(Health.UNAVAILABLE, ReasonCode.REQUIRED_COMPONENT_UNAVAILABLE)
        if any(item.health is Health.DEGRADED for item in required_items):
            return AggregateResult(Health.DEGRADED, ReasonCode.REQUIRED_COMPONENT_DEGRADED)
        if any(
            item.health is Health.UNKNOWN
            or item.provenance in {Provenance.NOT_CHECKED, Provenance.CONFIGURED, Provenance.STALE}
            for item in required_items
        ):
            return AggregateResult(Health.UNKNOWN, ReasonCode.REQUIRED_COMPONENT_UNKNOWN)

        # Liveness/readiness intentionally do not inherit failures from optional external systems.
        if projection is not Projection.HEALTH:
            return AggregateResult(Health.HEALTHY, ReasonCode.ALL_REQUIRED_HEALTHY)

        optional_items = [
            by_component[key]
            for key in policy.health_optional
            if key in by_component and by_component[key].applicability is Applicability.ENABLED
        ]
        if any(item.health in {Health.DEGRADED, Health.UNAVAILABLE} for item in optional_items):
            return AggregateResult(Health.DEGRADED, ReasonCode.OPTIONAL_COMPONENT_DEGRADED)
        if any(
            item.health is Health.UNKNOWN
            or item.provenance in {Provenance.NOT_CHECKED, Provenance.CONFIGURED, Provenance.STALE}
            for item in optional_items
        ):
            return AggregateResult(Health.UNKNOWN, ReasonCode.OPTIONAL_COMPONENT_UNKNOWN)
        return AggregateResult(Health.HEALTHY, ReasonCode.ALL_REQUIRED_HEALTHY)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observed_at": _timestamp_text(self.observed_at),
            "product_version": self.product_version,
            "process_role": self.process_role.value,
            "platform_family": self.platform_family.value,
            "components": [item.as_dict() for item in self.components],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ObservationSnapshot:
        schema = value.get("schema_version")
        if type(schema) is not int or schema != OBSERVATION_SCHEMA_VERSION:
            raise ValueError("unsupported observation schema version")
        role_value = value.get("process_role")
        platform_value = value.get("platform_family")
        try:
            role = ProcessRole(role_value) if isinstance(role_value, str) else None
            platform = PlatformFamily(platform_value) if isinstance(platform_value, str) else None
        except ValueError as exc:
            raise ValueError("unknown snapshot identity enum") from exc
        if role is None or platform is None:
            raise ValueError("snapshot identity enums are required")
        components_value = value.get("components")
        if not isinstance(components_value, list):
            raise ValueError("components must be a list")
        components: list[ComponentObservation] = []
        for item in components_value:
            if not isinstance(item, Mapping):
                raise ValueError("component observation must be an object")
            components.append(ComponentObservation.from_dict(item))
        version = value.get("product_version")
        if not isinstance(version, str):
            raise ValueError("product_version must be a string")
        return cls(
            schema_version=schema,
            observed_at=_parse_timestamp(value.get("observed_at")),
            product_version=version,
            process_role=role,
            platform_family=platform,
            components=tuple(components),
        )


class MetricType(StrEnum):
    GAUGE = "gauge"
    COUNTER = "counter"
    HISTOGRAM = "histogram"


_FORBIDDEN_DIMENSION_TOKENS = frozenset(
    {
        "task",
        "workdir",
        "path",
        "user",
        "principal",
        "model",
        "session",
        "correlation",
        "idempotency",
        "hostname",
        "host",
        "ip",
        "url",
        "file",
        "prompt",
        "error",
        "exception",
    }
)


@dataclass(frozen=True)
class DimensionSpec:
    name: str
    values: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name or any(
            token in self.name.lower() for token in _FORBIDDEN_DIMENSION_TOKENS
        ):
            raise ValueError("sensitive or invalid metric dimension name")
        if not self.values or len(self.values) > 128:
            raise ValueError("metric dimension must have a finite bounded domain")
        if any(not value or len(value) > 64 for value in self.values):
            raise ValueError("invalid metric dimension value")


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    metric_type: MetricType
    unit: str
    dimensions: tuple[DimensionSpec, ...]

    def __post_init__(self) -> None:
        if not self.name.startswith("serverfs_"):
            raise ValueError("metric name must use serverfs_ namespace")
        names = [item.name for item in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError("duplicate metric dimension")
        if self.series_ceiling > MAX_METRIC_SERIES:
            raise ValueError("metric series ceiling exceeds the process budget")

    @property
    def series_ceiling(self) -> int:
        return prod(len(item.values) for item in self.dimensions) if self.dimensions else 1

    def validate_labels(self, labels: Mapping[str, str]) -> None:
        expected = {item.name: item for item in self.dimensions}
        if set(labels) != set(expected):
            raise ValueError("metric labels do not match the catalog")
        for name, value in labels.items():
            if value not in expected[name].values:
                raise ValueError("metric label value is outside its bounded domain")


_COMPONENT_VALUES = frozenset(item.value for item in ComponentKey)
_HEALTH_VALUES = frozenset(item.value for item in Health)
_ROLE_VALUES = frozenset(item.value for item in ProcessRole)
_REASON_VALUES = frozenset(item.value for item in ReasonCode)
_RUNTIME_FAMILY_VALUES = frozenset({"codex", "claude", "qoder", "acp", "unknown"})
_MODE_VALUES = frozenset({"review", "workspace-write"})
_TASK_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_PROBE_OUTCOMES = frozenset({"success", "failure", "timeout", "skipped"})
_QUEUE_CLASSES = frozenset({"runnable", "waiting_interaction"})
_LEASE_OUTCOMES = frozenset({"acquired", "busy", "failure"})
_RECOVERY_ACTIONS = frozenset({"reconcile", "resume", "interrupt", "guard"})
_RECOVERY_OUTCOMES = frozenset({"success", "failure", "unknown"})
_TUNNEL_STATES = frozenset({"alive", "ready", "degraded", "unavailable", "unknown"})


def _dimension(name: str, values: frozenset[str]) -> DimensionSpec:
    return DimensionSpec(name=name, values=values)


METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "serverfs_component_state",
        MetricType.GAUGE,
        "state",
        (
            _dimension("component", _COMPONENT_VALUES),
            _dimension("state", _HEALTH_VALUES),
            _dimension("process_role", _ROLE_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_component_probe_total",
        MetricType.COUNTER,
        "total",
        (
            _dimension("component", _COMPONENT_VALUES),
            _dimension("outcome", _PROBE_OUTCOMES),
            _dimension("reason_code", _REASON_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_tasks_total",
        MetricType.COUNTER,
        "total",
        (
            _dimension("outcome", _TASK_OUTCOMES),
            _dimension("runtime_family", _RUNTIME_FAMILY_VALUES),
            _dimension("mode", _MODE_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_task_duration_seconds",
        MetricType.HISTOGRAM,
        "seconds",
        (
            _dimension("outcome", _TASK_OUTCOMES),
            _dimension("runtime_family", _RUNTIME_FAMILY_VALUES),
            _dimension("mode", _MODE_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_queue_depth",
        MetricType.GAUGE,
        "items",
        (
            _dimension("queue_class", _QUEUE_CLASSES),
            _dimension("runtime_family", _RUNTIME_FAMILY_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_queue_wait_seconds",
        MetricType.HISTOGRAM,
        "seconds",
        (
            _dimension("runtime_family", _RUNTIME_FAMILY_VALUES),
            _dimension("outcome", _TASK_OUTCOMES),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_lease_acquisitions_total",
        MetricType.COUNTER,
        "total",
        (
            _dimension("outcome", _LEASE_OUTCOMES),
            _dimension("lease_class", frozenset({"writer"})),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_lease_wait_seconds",
        MetricType.HISTOGRAM,
        "seconds",
        (
            _dimension("outcome", _LEASE_OUTCOMES),
            _dimension("lease_class", frozenset({"writer"})),
        ),
    ),
    MetricDefinition(
        "serverfs_agent_recovery_total",
        MetricType.COUNTER,
        "total",
        (
            _dimension("action", _RECOVERY_ACTIONS),
            _dimension("outcome", _RECOVERY_OUTCOMES),
            _dimension("reason_code", _REASON_VALUES),
        ),
    ),
    MetricDefinition(
        "serverfs_tunnel_state",
        MetricType.GAUGE,
        "state",
        (_dimension("state", _TUNNEL_STATES),),
    ),
)

METRIC_CATALOG_BY_NAME: dict[str, MetricDefinition] = {item.name: item for item in METRIC_CATALOG}
TOTAL_METRIC_SERIES_CEILING = sum(item.series_ceiling for item in METRIC_CATALOG)
if TOTAL_METRIC_SERIES_CEILING > MAX_METRIC_SERIES:
    raise RuntimeError("metric catalog exceeds the process series ceiling")


def validate_metric_labels(name: str, labels: Mapping[str, str]) -> None:
    try:
        definition = METRIC_CATALOG_BY_NAME[name]
    except KeyError as exc:
        raise ValueError("unknown metric") from exc
    definition.validate_labels(labels)
