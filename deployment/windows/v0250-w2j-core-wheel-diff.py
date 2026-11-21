"""Fail-closed, read-only differential proof for the one-file W2j Core fix.

W2f and W2j Product wheels must have exactly the same installed archive
members; only the Core mutex owner source file and wheel RECORD may differ.
No wheel or environment is mutated by this checker.
"""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

_TARGET = "serverfs_mcp/windows_core_control.py"
_RECORD_SUFFIX = ".dist-info/RECORD"
_MUTEX = re.compile(rb'"Local(\\+)serverfs-mcp-core-v1-"')


class WheelDiffError(ValueError):
    """Fixed failure without disclosing wheel/user paths."""


def _members(path: Path) -> dict[str, bytes]:
    if not path.is_file() or not path.name.endswith(".whl"):
        raise WheelDiffError("candidate Product Wheel is unavailable")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or len(names) < 25:
            raise WheelDiffError("wheel entries are duplicated or incomplete")
        if any(name.startswith("/") or "\\" in name or ".." in name.split("/") for name in names):
            raise WheelDiffError("wheel contains an invalid member path")
        return {name: archive.read(name) for name in names if not name.endswith("/")}


def verify(old: Path, new: Path, source: Path) -> None:
    previous = _members(old)
    updated = _members(new)
    if previous.keys() != updated.keys() or _TARGET not in previous:
        raise WheelDiffError("the candidate Wheel member list changed")
    changed = {name for name in previous if previous[name] != updated[name]}
    records = {name for name in updated if name.endswith(_RECORD_SUFFIX)}
    if len(records) != 1 or changed != {_TARGET} | records:
        raise WheelDiffError("wheel differs outside the one fixed Core module and RECORD")
    if hashlib.sha256(updated[_TARGET]).digest() != hashlib.sha256(source.read_bytes()).digest():
        raise WheelDiffError("the new Core module differs from reviewed source")
    old_match = _MUTEX.search(previous[_TARGET])
    new_match = _MUTEX.search(updated[_TARGET])
    if not old_match or not new_match:
        raise WheelDiffError("Core mutex namespace source signature is unavailable")
    if len(old_match.group(1)) != 4 or len(new_match.group(1)) != 2:
        raise WheelDiffError("Core mutex namespace correction is not exact")
    metadata = [key for key in updated if key.endswith(".dist-info/METADATA")]
    if len(metadata) != 1 or previous[metadata[0]] != updated[metadata[0]]:
        raise WheelDiffError("Wheel metadata differs from the accepted W2f baseline")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("W2J_WHEEL_DIFF=FAILED (arguments)", file=sys.stderr)
        return 2
    try:
        verify(Path(argv[0]), Path(argv[1]), Path(argv[2]))
    except Exception as exc:  # noqa: BLE001 - only safe error class, not private paths
        print(f"W2J_WHEEL_DIFF=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2
    print("W2J_WHEEL_DIFF=PASS (Core mutex module plus RECORD only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
