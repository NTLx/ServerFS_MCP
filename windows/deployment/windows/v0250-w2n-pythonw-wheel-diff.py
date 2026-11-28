"""W2n read-only candidate Product wheel provenance and source-byte proof.

Compared with the W2j installed Core Product wheel, the W2n candidate
must change EXACTLY four reviewed modules plus wheel RECORD. No packages,
version metadata, entrypoints or unrelated source may change.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

_MODULES = frozenset(
    {
        "serverfs_mcp/windows_process.py",
        "serverfs_mcp/windows_core_control.py",
        "serverfs_mcp/windows_core_listener.py",
        "serverfs_mcp/windows_connector_control.py",
    }
)
_RECORD_SUFFIX = ".dist-info/RECORD"


class W2nDiffError(ValueError):
    """Fixed, non-secret candidate provenance refusal."""


def _entries(path: Path) -> dict[str, bytes]:
    if not path.is_file() or path.suffix != ".whl":
        raise W2nDiffError("Product wheel input is absent")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if (
            len(names) < 25
            or len(set(names)) != len(names)
            or any(
                not name
                or name.startswith("/")
                or "\\" in name
                or ".." in name.split("/")
                or name.endswith("/")
                for name in names
            )
        ):
            raise W2nDiffError("Product wheel archive layout is invalid")
        return {name: wheel.read(name) for name in names}


def verify(old: Path, new: Path, checkout_source: Path) -> None:
    before = _entries(old)
    after = _entries(new)
    if set(before) != set(after) or not _MODULES <= set(before):
        raise W2nDiffError("candidate wheel members are not identical")
    record = {name for name in after if name.endswith(_RECORD_SUFFIX)}
    changed = {name for name in before if before[name] != after[name]}
    if len(record) != 1 or changed != _MODULES | record:
        raise W2nDiffError("unexpected wheel source or metadata difference")
    for member in _MODULES:
        source = checkout_source / "src" / member
        if (
            not source.is_file()
            or hashlib.sha256(after[member]).digest()
            != hashlib.sha256(source.read_bytes()).digest()
        ):
            raise W2nDiffError("candidate module bytes differ from reviewed source")
    metadata = [name for name in before if name.endswith(".dist-info/METADATA")]
    if len(metadata) != 1 or before[metadata[0]] != after[metadata[0]]:
        raise W2nDiffError("candidate wheel product metadata changed")


def main(args: list[str]) -> int:
    if len(args) != 3:
        print("W2N_WHEEL_DIFF=FAILED (arguments)", file=sys.stderr)
        return 2
    try:
        verify(Path(args[0]), Path(args[1]), Path(args[2]))
    except Exception as exc:  # noqa: BLE001 - exclude private path from errors
        print(f"W2N_WHEEL_DIFF=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2
    print("W2N_WHEEL_DIFF=PASS (four reviewed modules and RECORD only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
