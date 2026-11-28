"""Read-only Linux private-state inspection.

Preserve the existing four-status redacted diagnostic contract, with native
UID, mode and object-type checks. Never create or modify state.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import BridgeError

#: The only four values any field of the report may take.
ABSENT = "absent"
SAFE = "safe"
UNSAFE = "unsafe"
UNKNOWN = "unknown"

#: The keys of the report, in the order a reader wants them.
STATE_DIR = "state"
LOCK_DIR = "locks"
CONFIG_FILE = "config"

__all__ = [
    "ABSENT",
    "CONFIG_FILE",
    "LOCK_DIR",
    "SAFE",
    "STATE_DIR",
    "UNKNOWN",
    "UNSAFE",
    "StateReport",
    "inspect_private_state",
    "main",
]


@dataclass(frozen=True)
class StateReport:
    """Bounded structural status of one private-state location."""

    status: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        """The wire form. Two short fields, no path and no descriptor detail."""
        return {"status": self.status, "reason": self.reason}


def _absent(what: str) -> StateReport:
    return StateReport(ABSENT, f"{what} has not been created yet")


def _safe(what: str) -> StateReport:
    return StateReport(SAFE, f"{what} is private and correctly typed")


def _unsafe(what: str, reason: str) -> StateReport:
    return StateReport(UNSAFE, reason)


def _unknown(what: str, reason: str) -> StateReport:
    return StateReport(UNKNOWN, reason)


def _inspect_posix_directory(path: Path, what: str) -> StateReport:
    """Classify one POSIX directory by owner and mode bits (Linux and macOS v0.13).

    The same rules the live path enforces through ``private_state``'s POSIX
    branch: a real directory (no symlink), owned by the Bridge user, with no
    group or world access. Same four-status vocabulary, same redaction.
    """
    try:
        lstatted = path.lstat()
    except FileNotFoundError:
        return _absent(what)
    except OSError:
        return _unknown(what, "the entry could not be examined")
    if stat.S_ISLNK(lstatted.st_mode) or not stat.S_ISDIR(lstatted.st_mode):
        return _unsafe(what, "is not a directory")
    if lstatted.st_uid != os.getuid():
        return _unsafe(what, "has a different owner")
    if lstatted.st_mode & 0o077:
        return _unsafe(what, "grants access beyond the Bridge user")
    return _safe(what)


def _inspect_posix_file(path: Path, what: str) -> StateReport:
    """Classify one POSIX regular file by owner and mode bits."""
    try:
        lstatted = path.lstat()
    except FileNotFoundError:
        return _absent(what)
    except OSError:
        return _unknown(what, "the entry could not be examined")
    if stat.S_ISLNK(lstatted.st_mode) or not stat.S_ISREG(lstatted.st_mode):
        return _unsafe(what, "is not a regular file")
    if lstatted.st_uid != os.getuid():
        return _unsafe(what, "has a different owner")
    if lstatted.st_mode & 0o077:
        return _unsafe(what, "grants access beyond the Bridge user")
    return _safe(what)


def inspect_private_state(data_home: Path) -> dict[str, dict[str, str]]:
    """Classify the four private-state locations under one data home.

    Nothing is created. A cold deployment reports four ``absent`` entries, which is a healthy
    answer -- an inspector that turned a missing directory into a fault would make a fresh install
    look broken.
    """
    return {
        "data_home": _inspect_posix_directory(
            Path(data_home) / "agent-bridge", "the Agent data home"
        ).as_dict(),
        STATE_DIR: _inspect_posix_directory(
            Path(data_home) / "agent-bridge" / "state", "the Bridge state directory"
        ).as_dict(),
        LOCK_DIR: _inspect_posix_directory(
            Path(data_home) / "agent-bridge" / "locks", "the writer lock directory"
        ).as_dict(),
        CONFIG_FILE: _inspect_posix_file(
            Path(data_home) / "agent-bridge" / "bridge.json", "the rendered Bridge config"
        ).as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    """Report one data home as JSON on stdout. Human-readable text goes on stderr.

    JSON on stdout is what lets the MCP side consume the result structurally instead of parsing
    prose, and it is why the report carries no path: the caller already knows which data home it
    asked about, and a path in the output would be one more thing to redact later.
    """
    parser = argparse.ArgumentParser(description="Inspect ServerFS Agent Bridge private state")
    parser.add_argument("--data-home", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        report = inspect_private_state(args.data_home)
    except (OSError, BridgeError) as exc:
        # A total failure is still a bounded answer: the caller learns nothing leaked.
        print(json.dumps({"error": type(exc).__name__}), file=sys.stdout)
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
