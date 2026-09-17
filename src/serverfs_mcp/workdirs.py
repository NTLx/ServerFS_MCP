"""Workdir registry.

Reads WORKDIR_XX_ALIAS / WORKDIR_XX_DESCRIPTION from the environment and the
disabled sentinel file /workdirs/XX/.serverfs-disabled to build the set of
enabled workdirs. Validation failures raise WorkdirError with a message
suitable for both logs and startup exit.
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


class WorkdirRegistry:
    """Immutable registry of enabled workdirs built once at startup."""

    def __init__(self, workdirs: list[Workdir]):
        self._by_alias = {w.alias: w for w in workdirs}
        self._all = list(workdirs)

    def get(self, alias: str) -> Workdir | None:
        return self._by_alias.get(alias)

    def list_result(self) -> ListWorkdirsResult:
        return ListWorkdirsResult(
            workdirs=[WorkdirInfo(alias=w.alias, description=w.description) for w in self._all]
        )

    def __len__(self) -> int:
        return len(self._all)


def build_registry(
    env_alias: dict[int, str],
    env_description: dict[int, str],
    workdir_root: Path = WORKDIR_ROOT,
) -> WorkdirRegistry:
    """Validate all 16 slots and build the registry.

    env_alias / env_description map slot number -> raw env value (may be
    empty). workdir_root is injectable for tests.
    """
    workdirs: list[Workdir] = []
    seen_aliases: dict[str, int] = {}

    for slot in range(1, SLOT_COUNT + 1):
        alias = env_alias.get(slot, "").strip()
        description = env_description.get(slot, "").strip() or None
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

        if alias in seen_aliases:
            raise WorkdirError(
                f"slot {slot:02d}: duplicate alias '{alias}' "
                f"(also configured in slot {seen_aliases[alias]:02d})."
            )
        seen_aliases[alias] = slot

        workdirs.append(
            Workdir(slot=slot, alias=alias, container_path=slot_path, description=description)
        )

    return WorkdirRegistry(workdirs)


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
