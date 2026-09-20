"""Workdir registry.

Reads WORKDIR_XX_ALIAS / WORKDIR_XX_DESCRIPTION / WORKDIR_XX_READ_ONLY plus
optional WORKDIR_XX_AGENT_MODE / WORKDIR_XX_AGENT_RUNTIMES from
the environment and the disabled sentinel file /workdirs/XX/.serverfs-disabled
to build the set of enabled workdirs. Validation failures raise WorkdirError
with a message suitable for both logs and startup exit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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
class Workdir:
    slot: int
    alias: str
    container_path: Path
    description: str | None
    read_only: bool = True
    agent_mode: str = AGENT_MODE_DISABLED
    agent_runtimes: frozenset[str] = frozenset()

    @property
    def access(self) -> str:
        """Agent-facing access mode derived from the authorization flag."""
        return ACCESS_READ_ONLY if self.read_only else ACCESS_READ_WRITE


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
                WorkdirInfo(alias=w.alias, description=w.description, access=w.access)
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
    """Validate all 16 slots and build the registry.

    env_alias / env_description / env_read_only / env_agent_mode /
    env_agent_runtimes map slot number -> raw env value (may be empty). A missing
    env_read_only mapping (or an empty value
    for a slot) means read-only: configurations written for v0.1, which had
    no such variable, therefore upgrade read-only. workdir_root is injectable
    for tests.
    """
    workdirs: list[Workdir] = []
    seen_aliases: dict[str, int] = {}
    read_only_env = env_read_only or {}
    agent_mode_env = env_agent_mode or {}
    agent_runtimes_env = env_agent_runtimes or {}

    for slot in range(1, SLOT_COUNT + 1):
        alias = env_alias.get(slot, "").strip()
        description = env_description.get(slot, "").strip() or None
        read_only = parse_read_only(slot, read_only_env.get(slot, ""))
        agent_mode = parse_agent_mode(slot, agent_mode_env.get(slot, ""))
        agent_runtimes = parse_agent_runtimes(slot, agent_runtimes_env.get(slot, ""))
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
            if agent_mode != AGENT_MODE_DISABLED or agent_runtimes:
                raise WorkdirError(
                    f"slot {slot:02d}: disabled slot cannot enable Agent delegation. "
                    f"Clear WORKDIR_{slot:02d}_AGENT_MODE and "
                    f"WORKDIR_{slot:02d}_AGENT_RUNTIMES."
                )
            continue  # case A: normally disabled slot

        if sentinel_present:
            # case B: alias set but no host path bound
            raise WorkdirError(
                f"slot {slot:02d}: alias '{alias}' is set but the slot is "
                f"disabled (no path bound). Set WORKDIR_{slot:02d}_PATH in .env."
            )

        if not ALIAS_RE.fullmatch(alias):
            raise WorkdirError(
                f"slot {slot:02d}: invalid alias '{alias}'. Aliases must match "
                "^[A-Za-z][A-Za-z0-9_-]{0,31}$ (letter first, up to 32 chars, "
                "no slashes or spaces)."
            )

        if agent_mode == AGENT_MODE_DISABLED and agent_runtimes:
            raise WorkdirError(
                f"slot {slot:02d}: WORKDIR_{slot:02d}_AGENT_RUNTIMES requires an enabled "
                "WORKDIR_XX_AGENT_MODE"
            )
        if agent_mode != AGENT_MODE_DISABLED and not agent_runtimes:
            raise WorkdirError(
                f"slot {slot:02d}: enabled Agent mode requires WORKDIR_{slot:02d}_AGENT_RUNTIMES"
            )
        if agent_mode == AGENT_MODE_WORKSPACE_WRITE and read_only:
            raise WorkdirError(
                f"slot {slot:02d}: workspace-write Agent mode requires "
                f"WORKDIR_{slot:02d}_READ_ONLY=false"
            )
        if agent_runtimes & {"codex", "claude"} and agent_mode != AGENT_MODE_WORKSPACE_WRITE:
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
                agent_mode=agent_mode,
                agent_runtimes=agent_runtimes,
            )
        )

    return WorkdirRegistry(workdirs)


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
