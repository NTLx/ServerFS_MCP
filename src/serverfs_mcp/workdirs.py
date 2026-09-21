"""Workdir registry.

Reads WORKDIR_XX_ALIAS / WORKDIR_XX_DESCRIPTION / WORKDIR_XX_READ_ONLY plus
optional WORKDIR_XX_AGENT_MODE / WORKDIR_XX_AGENT_RUNTIMES from
the environment and the disabled sentinel file /workdirs/XX/.serverfs-disabled
to build the set of enabled workdirs. Validation failures raise WorkdirError
with a message suitable for both logs and startup exit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import ListWorkdirsResult, WorkdirInfo

SLOT_COUNT = 16
DISABLED_SENTINEL = ".serverfs-disabled"
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
WORKDIR_ROOT = Path("/workdirs")

# Strict booleans for WORKDIR_XX_READ_ONLY: a security switch must never
# fail open, so anything outside this set aborts startup.
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})

ACCESS_READ_ONLY = "read-only"
ACCESS_READ_WRITE = "read-write"

AGENT_MODE_DISABLED = "disabled"
AGENT_MODE_REVIEW = "review"
AGENT_MODE_WORKSPACE_WRITE = "workspace-write"
AGENT_MODES = frozenset({AGENT_MODE_DISABLED, AGENT_MODE_REVIEW, AGENT_MODE_WORKSPACE_WRITE})
PUBLIC_AGENT_RUNTIMES = frozenset({"codex", "claude"})

SENTINEL_CONFLICT_MSG = (
    "workdir root for slot {slot} contains the reserved file "
    f"'{DISABLED_SENTINEL}'. This name is reserved for disabled-slot "
    "detection; refusing to start. Remove or rename the file on the host."
)


class WorkdirError(Exception):
    """Configuration error that must abort startup."""


@dataclass(frozen=True)
class EffectiveWorkdirPolicy:
    """The resolved, immutable policy used by one workdir."""

    allow_hidden: bool = False
    disable_default_deny: bool = False
    extra_deny_globs: tuple[str, ...] = ()
    max_read_bytes: int = 524_288
    max_read_lines: int = 500
    max_write_bytes: int = 1_048_576
    binary_transfer_enabled: bool = False
    max_binary_transfer_bytes: int = 8_388_608
    agent_mode: str = AGENT_MODE_DISABLED
    agent_runtimes: frozenset[str] = frozenset()


@dataclass(frozen=True, init=False)
class Workdir:
    slot: int
    alias: str
    container_path: Path
    description: str | None
    read_only: bool = True
    policy: EffectiveWorkdirPolicy

    def __init__(
        self,
        slot: int,
        alias: str,
        container_path: Path,
        description: str | None,
        read_only: bool = True,
        *,
        policy: EffectiveWorkdirPolicy | None = None,
        agent_mode: str | None = None,
        agent_runtimes: frozenset[str] | None = None,
    ) -> None:
        """Construct a workdir; legacy agent kwargs are folded into policy once."""
        effective_policy = policy or EffectiveWorkdirPolicy()
        if agent_mode is not None or agent_runtimes is not None:
            effective_policy = EffectiveWorkdirPolicy(
                allow_hidden=effective_policy.allow_hidden,
                disable_default_deny=effective_policy.disable_default_deny,
                extra_deny_globs=effective_policy.extra_deny_globs,
                max_read_bytes=effective_policy.max_read_bytes,
                max_read_lines=effective_policy.max_read_lines,
                max_write_bytes=effective_policy.max_write_bytes,
                binary_transfer_enabled=effective_policy.binary_transfer_enabled,
                max_binary_transfer_bytes=effective_policy.max_binary_transfer_bytes,
                agent_mode=agent_mode if agent_mode is not None else effective_policy.agent_mode,
                agent_runtimes=(
                    agent_runtimes
                    if agent_runtimes is not None
                    else effective_policy.agent_runtimes
                ),
            )
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "container_path", container_path)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "read_only", read_only)
        object.__setattr__(self, "policy", effective_policy)

    @property
    def access(self) -> str:
        """Agent-facing access mode derived from the authorization flag."""
        return ACCESS_READ_ONLY if self.read_only else ACCESS_READ_WRITE

    @property
    def agent_mode(self) -> str:
        return self.policy.agent_mode

    @property
    def agent_runtimes(self) -> frozenset[str]:
        return self.policy.agent_runtimes


class WorkdirRegistry:
    """Immutable registry of enabled workdirs built once at startup."""

    def __init__(self, workdirs: list[Workdir]):
        self._by_alias = {w.alias: w for w in workdirs}
        self._all = list(workdirs)

    def get(self, alias: str) -> Workdir | None:
        return self._by_alias.get(alias)

    def list_result(self) -> ListWorkdirsResult:
        return ListWorkdirsResult(
            workdirs=[
                WorkdirInfo(
                    alias=w.alias,
                    description=w.description,
                    access=w.access,
                    binary_transfer=w.policy.binary_transfer_enabled,
                    agent_mode=w.policy.agent_mode,
                    agent_runtimes=sorted(w.policy.agent_runtimes),
                )
                for w in self._all
            ]
        )

    def all_workdirs(self) -> tuple[Workdir, ...]:
        """Internal immutable view used for local Agent authorization."""
        return tuple(self._all)

    def __len__(self) -> int:
        return len(self._all)


def parse_read_only(slot: int, raw: str) -> bool:
    """Parse WORKDIR_XX_READ_ONLY strictly; empty means the safe default.

    Accepts (case-insensitive) true/false/1/0/yes/no/on/off. Any other value
    is a configuration error: guessing would silently turn a read-only
    workdir writable, or the reverse.
    """
    value = raw.strip().lower()
    if not value:
        return True
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise WorkdirError(
        f"slot {slot:02d}: invalid WORKDIR_{slot:02d}_READ_ONLY value {raw.strip()!r}. "
        "Use true or false (also accepted: 1/0, yes/no, on/off)."
    )


def build_registry(
    env_alias: dict[int, str],
    env_description: dict[int, str],
    env_read_only: dict[int, str] | None = None,
    env_agent_mode: dict[int, str] | None = None,
    env_agent_runtimes: dict[int, str] | None = None,
    *,
    workdir_root: Path = WORKDIR_ROOT,
) -> WorkdirRegistry:
    """Compatibility helper delegating to the environment-based builder."""
    env: dict[str, str] = {}
    for slot in range(1, SLOT_COUNT + 1):
        prefix = f"WORKDIR_{slot:02d}_"
        env[prefix + "ALIAS"] = (env_alias or {}).get(slot, "")
        env[prefix + "DESCRIPTION"] = (env_description or {}).get(slot, "")
        env[prefix + "READ_ONLY"] = (env_read_only or {}).get(slot, "")
        env[prefix + "AGENT_MODE"] = (env_agent_mode or {}).get(slot, "")
        env[prefix + "AGENT_RUNTIMES"] = (env_agent_runtimes or {}).get(slot, "")
    return _build_registry(env, Settings(), workdir_root=workdir_root)


def build_registry_from_env(
    env: Mapping[str, str], settings: Settings, *, workdir_root: Path = WORKDIR_ROOT
) -> WorkdirRegistry:
    """Resolve all global and WORKDIR_XX policy values once at startup."""
    return _build_registry(env, settings, workdir_root=workdir_root)


def _build_registry(
    env: Mapping[str, str], settings: Settings, *, workdir_root: Path
) -> WorkdirRegistry:
    """Shared implementation for production and the legacy test helper."""
    workdirs: list[Workdir] = []
    seen_aliases: dict[str, int] = {}

    for slot in range(1, SLOT_COUNT + 1):
        prefix = f"WORKDIR_{slot:02d}_"
        alias = env.get(prefix + "ALIAS", "").strip()
        description = env.get(prefix + "DESCRIPTION", "").strip() or None
        read_only = parse_read_only(slot, env.get(prefix + "READ_ONLY", ""))
        slot_path = workdir_root / f"{slot:02d}"
        sentinel = slot_path / DISABLED_SENTINEL
        sentinel_present = _is_sentinel(slot, slot_path, sentinel, alias)

        if not alias:
            if not sentinel_present:
                # case C: host path mounted but no alias configured
                raise WorkdirError(
                    f"slot {slot:02d}: workdir path is configured "
                    f"(no '{DISABLED_SENTINEL}' present) but alias is empty. "
                    f"Set WORKDIR_{slot:02d}_ALIAS."
                )
            if not read_only:
                raise WorkdirError(
                    f"slot {slot:02d}: disabled slot has "
                    f"WORKDIR_{slot:02d}_READ_ONLY=false. A slot without an alias "
                    "cannot be written to; set it back to true or remove the "
                    "variable."
                )
            _validate_disabled_slot_overrides(slot, env)
            continue  # case A: normally disabled slot

        if sentinel_present:
            # case B: alias set but no host path bound
            raise WorkdirError(
                f"slot {slot:02d}: alias '{alias}' is set but the slot is "
                f"disabled (no path bound). Set WORKDIR_{slot:02d}_PATH in .env."
            )

        policy = _policy_for_slot(slot, env, settings)

        if not ALIAS_RE.fullmatch(alias):
            raise WorkdirError(
                f"slot {slot:02d}: invalid alias '{alias}'. Aliases must match "
                "^[A-Za-z][A-Za-z0-9_-]{0,31}$ (letter first, up to 32 chars, "
                "no slashes or spaces)."
            )

        if policy.agent_mode == AGENT_MODE_DISABLED and policy.agent_runtimes:
            raise WorkdirError(
                f"slot {slot:02d}: WORKDIR_{slot:02d}_AGENT_RUNTIMES requires an enabled "
                "WORKDIR_XX_AGENT_MODE"
            )
        if policy.agent_mode != AGENT_MODE_DISABLED and not policy.agent_runtimes:
            raise WorkdirError(
                f"slot {slot:02d}: enabled Agent mode requires WORKDIR_{slot:02d}_AGENT_RUNTIMES"
            )
        if policy.agent_mode == AGENT_MODE_WORKSPACE_WRITE and read_only:
            raise WorkdirError(
                f"slot {slot:02d}: workspace-write Agent mode requires "
                f"WORKDIR_{slot:02d}_READ_ONLY=false"
            )
        if (
            policy.agent_runtimes & {"codex", "claude"}
            and policy.agent_mode != AGENT_MODE_WORKSPACE_WRITE
        ):
            raise WorkdirError(
                f"slot {slot:02d}: Codex/Claude native mode currently requires "
                "WORKDIR_XX_AGENT_MODE=workspace-write"
            )

        if alias in seen_aliases:
            raise WorkdirError(
                f"slot {slot:02d}: duplicate alias '{alias}' "
                f"(also configured in slot {seen_aliases[alias]:02d})."
            )
        seen_aliases[alias] = slot

        workdirs.append(
            Workdir(
                slot=slot,
                alias=alias,
                container_path=slot_path,
                description=description,
                read_only=read_only,
                policy=policy,
            )
        )

    return WorkdirRegistry(workdirs)


def _policy_for_slot(
    slot: int, env: Mapping[str, str], settings: Settings
) -> EffectiveWorkdirPolicy:
    prefix = f"WORKDIR_{slot:02d}_"

    def raw(name: str) -> str:
        return env.get(prefix + name, "").strip()

    agent_mode_raw = raw("AGENT_MODE")
    agent_runtimes_raw = raw("AGENT_RUNTIMES")
    if agent_mode_raw:
        agent_mode = parse_agent_mode(slot, agent_mode_raw)
        # An explicit disabled mode is also an explicit reset of runtimes.
        agent_runtimes = (
            frozenset()
            if agent_mode == AGENT_MODE_DISABLED and not agent_runtimes_raw
            else _override_runtimes(slot, agent_runtimes_raw, settings.agent_runtimes)
        )
    else:
        agent_mode = settings.agent_mode
        agent_runtimes = _override_runtimes(slot, agent_runtimes_raw, settings.agent_runtimes)

    return EffectiveWorkdirPolicy(
        allow_hidden=_override_bool(
            slot, prefix + "ALLOW_HIDDEN", raw("ALLOW_HIDDEN"), settings.allow_hidden
        ),
        disable_default_deny=_override_bool(
            slot,
            prefix + "DISABLE_DEFAULT_DENY",
            raw("DISABLE_DEFAULT_DENY"),
            settings.disable_default_deny,
        ),
        extra_deny_globs=_union_globs(settings.extra_deny_globs, raw("EXTRA_DENY_GLOBS")),
        max_read_bytes=_override_int(
            slot, prefix + "MAX_READ_BYTES", raw("MAX_READ_BYTES"), settings.max_read_bytes
        ),
        max_read_lines=_override_int(
            slot, prefix + "MAX_READ_LINES", raw("MAX_READ_LINES"), settings.max_read_lines
        ),
        max_write_bytes=_override_int(
            slot, prefix + "MAX_WRITE_BYTES", raw("MAX_WRITE_BYTES"), settings.max_write_bytes
        ),
        binary_transfer_enabled=_override_bool(
            slot,
            prefix + "BINARY_TRANSFER_ENABLED",
            raw("BINARY_TRANSFER_ENABLED"),
            settings.binary_transfer_enabled,
        ),
        max_binary_transfer_bytes=_override_int(
            slot,
            prefix + "MAX_BINARY_TRANSFER_BYTES",
            raw("MAX_BINARY_TRANSFER_BYTES"),
            settings.max_binary_transfer_bytes,
        ),
        agent_mode=agent_mode,
        agent_runtimes=agent_runtimes,
    )


def _validate_disabled_slot_overrides(slot: int, env: Mapping[str, str]) -> None:
    prefix = f"WORKDIR_{slot:02d}_"
    agent_mode_raw = env.get(prefix + "AGENT_MODE", "").strip()
    agent_runtimes_raw = env.get(prefix + "AGENT_RUNTIMES", "").strip()
    binary_raw = env.get(prefix + "BINARY_TRANSFER_ENABLED", "").strip()
    if agent_mode_raw:
        agent_mode = parse_agent_mode(slot, agent_mode_raw)
        if agent_mode != AGENT_MODE_DISABLED:
            raise WorkdirError(
                f"slot {slot:02d}: disabled slot cannot enable Agent delegation. "
                f"Clear WORKDIR_{slot:02d}_AGENT_MODE and "
                f"WORKDIR_{slot:02d}_AGENT_RUNTIMES."
            )
    if agent_runtimes_raw:
        parse_agent_runtimes(slot, agent_runtimes_raw)
        raise WorkdirError(
            f"slot {slot:02d}: disabled slot cannot enable Agent delegation. "
            f"Clear WORKDIR_{slot:02d}_AGENT_MODE and WORKDIR_{slot:02d}_AGENT_RUNTIMES."
        )
    if binary_raw:
        if binary_raw.lower() not in _TRUE_VALUES | _FALSE_VALUES:
            raise WorkdirError(
                f"slot {slot:02d}: invalid {prefix}BINARY_TRANSFER_ENABLED value {binary_raw!r}"
            )
        if binary_raw.lower() in _TRUE_VALUES:
            raise WorkdirError(f"slot {slot:02d}: disabled slot cannot enable binary transfer")


def _override_bool(slot: int, key: str, raw: str, default: bool) -> bool:
    if not raw:
        return default
    if raw.lower() in _TRUE_VALUES:
        return True
    if raw.lower() in _FALSE_VALUES:
        return False
    raise WorkdirError(f"slot {slot:02d}: invalid {key} value {raw!r}")


def _override_int(slot: int, key: str, raw: str, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkdirError(f"slot {slot:02d}: invalid {key} value {raw!r}") from exc
    if value <= 0:
        raise WorkdirError(f"slot {slot:02d}: invalid {key} value {raw!r}")
    return value


def _union_globs(global_globs: tuple[str, ...], raw: str) -> tuple[str, ...]:
    local = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(dict.fromkeys((*global_globs, *local)))


def _override_runtimes(slot: int, raw: str, default: frozenset[str]) -> frozenset[str]:
    return parse_agent_runtimes(slot, raw) if raw else default


def parse_agent_mode(slot: int, raw: str) -> str:
    value = raw.strip().lower()
    if not value:
        return AGENT_MODE_DISABLED
    if value not in AGENT_MODES:
        raise WorkdirError(
            f"slot {slot:02d}: invalid WORKDIR_{slot:02d}_AGENT_MODE value {raw.strip()!r}. "
            "Use disabled, review or workspace-write."
        )
    return value


def parse_agent_runtimes(slot: int, raw: str) -> frozenset[str]:
    if not raw.strip():
        return frozenset()
    values = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if len(values) != len(set(values)):
        raise WorkdirError(f"slot {slot:02d}: duplicate Agent runtime")
    unknown = set(values) - PUBLIC_AGENT_RUNTIMES
    if unknown:
        raise WorkdirError(
            f"slot {slot:02d}: unknown WORKDIR_{slot:02d}_AGENT_RUNTIMES value: "
            + ", ".join(sorted(unknown))
        )
    return frozenset(values)


def _is_sentinel(slot: int, slot_path: Path, sentinel: Path, alias: str) -> bool:
    """Detect the sentinel, distinguishing 'real mounted dir happens to
    contain the reserved file' (fatal) from the disabled placeholder."""
    if not sentinel.exists():
        return False
    if not slot_path.is_symlink():
        # /workdirs/XX is a bind mount, never a symlink in production. In the
        # disabled case the whole directory is the repo's .empty placeholder.
        # Distinguish by content: a placeholder holds only the sentinel.
        entries = [p.name for p in slot_path.iterdir()]
        if entries != [DISABLED_SENTINEL]:
            raise WorkdirError(SENTINEL_CONFLICT_MSG.format(slot=f"{slot:02d}"))
        return True
    raise WorkdirError(SENTINEL_CONFLICT_MSG.format(slot=f"{slot:02d}"))
