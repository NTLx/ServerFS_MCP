"""Read-only inspection of the Bridge's private state, for diagnostics.

This module exists so ``serverfs doctor`` can answer "is the private state on disk safe?" without
reimplementing a single line of the ACL contract. §23/§70 freeze the two packages as independent,
and duplicating the Windows DACL rules inside ``serverfs_mcp`` would create a second answer that
could drift from the first -- the failure mode Phase B already demonstrated with the lease identity.

So the knowledge stays here, next to ``windows_security`` and ``private_state``, and the MCP side
reaches it through a bounded subprocess that runs under a Bridge interpreter. What crosses the
boundary is a bounded structural status and nothing else.

**The whole module is read-only.** It creates no directory, opens no SQLite database, repairs no
descriptor and starts no Bridge. That is not a promise, it is enforced three ways: nothing here
calls a create or chmod primitive, the only filesystem calls are ``lstat`` and
``read_object_security``, and the test suite runs against a tree it asserts is unchanged afterwards.

The output vocabulary is deliberately impoverished. A diagnostic that could print a path, a SID or
an ACL would eventually be pasted into a transcript or a bug report, so the inspector returns four
state names and nothing more:

``absent``
    The object does not exist yet. A cold deployment is correct, not a fault.
``safe``
    It exists, it is the right kind of object, and its descriptor grants the Bridge user and nobody
    else -- verified with the same ``_assert_windows_private`` the live path uses.
``unsafe``
    It exists and something is wrong: a reparse point, the wrong object type, another owner, a broad
    or foreign DACL.
``unknown``
    Inspection itself failed. This is the fail-closed value: an inspector that cannot tell must not
    report safety it did not verify.

``unknown`` is separated from ``unsafe`` on purpose. The first means "look again"; the second means
"this deployment is wrong". Collapsing them would train an operator to ignore the distinction, and
the distinction is the whole value of a diagnostic.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import private_state
from .errors import BridgeError

# ``windows_security`` loads kernel32/advapi32 while being imported, so it is pulled in lazily
# rather than at module scope: this module must stay importable off Windows so the CLI can answer
# "not defined here" instead of the whole process failing to start. ``private_state`` guards the
# same seam with its own ``WINDOWS`` flag and is safe to import directly.

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


def _inspect_directory(path: Path, what: str) -> StateReport:
    """Classify one directory without touching it."""
    from . import windows_security

    # Reparose first, always: Path.exists() follows a link, so a dangling reparse point reports
    # False and would otherwise be classified as a not-yet-created directory. The order is the
    # same one the whole Windows private-state seam now uses.
    try:
        if windows_security.is_reparse_point(path):
            return _unsafe(what, "is a reparse point")
    except (OSError, BridgeError):
        return _unknown(what, "reparse status could not be determined")

    try:
        lstatted = path.lstat()
    except FileNotFoundError:
        return _absent(what)
    except OSError:
        return _unknown(what, "the entry could not be examined")
    if not stat.S_ISDIR(lstatted.st_mode):
        return _unsafe(what, "is not a directory")

    # The descriptor verdict comes from the live assertion, so a diagnostic cannot disagree with
    # startup about what "private" means. It is only reachable on an existing object, which is why
    # it runs after the type check rather than instead of it.
    try:
        private_state._assert_windows_private(  # noqa: SLF001 - deliberately one implementation
            windows_security.read_object_security(path),
            windows_security.current_token_owner_sid(),
            protected=False,
        )
    except BridgeError as exc:
        return _unsafe(what, _reason_of(exc))
    except (OSError, ValueError):
        return _unknown(what, "the directory descriptor could not be verified")
    return _safe(what)


def _inspect_file(path: Path, what: str) -> StateReport:
    """Classify one regular file without opening it."""
    from . import windows_security

    try:
        if windows_security.is_reparse_point(path):
            return _unsafe(what, "is a reparse point")
    except (OSError, BridgeError):
        return _unknown(what, "reparse status could not be determined")

    try:
        lstatted = path.lstat()
    except FileNotFoundError:
        return _absent(what)
    except OSError:
        return _unknown(what, "the entry could not be examined")
    if not stat.S_ISREG(lstatted.st_mode):
        return _unsafe(what, "is not a regular file")

    try:
        private_state._assert_windows_private(  # noqa: SLF001 - one implementation, not two
            windows_security.read_object_security(path),
            windows_security.current_token_owner_sid(),
            protected=False,
        )
    except BridgeError as exc:
        return _unsafe(what, _reason_of(exc))
    except (OSError, ValueError):
        return _unknown(what, "the file descriptor could not be verified")
    return _safe(what)


def _reason_of(exc: BridgeError) -> str:
    """Reduce a refusal to a fixed phrase, keeping the word "unsafe" and dropping the specifics.

    ``_assert_windows_private`` names the offending trustee or ACE in its message, which is
    precisely what must not reach a diagnostic line. The classification is kept; the evidence is
    dropped.
    """
    text = str(exc)
    if "another owner" in text:
        return "has a different owner"
    if "does not grant the Bridge user" in text:
        return "no longer grants the Bridge user"
    # "grants access to <trustee>" is the broad-ACE refusal, and the trustee name is exactly the
    # text that must not reach a report line, so the phrase is matched and the name dropped.
    if "grants access to" in text or "another trustee" in text or "non-allow ACE" in text:
        return "grants access beyond the Bridge user"
    if "no explicit DACL" in text:
        return "has no explicit descriptor"
    if "inherits an unexpected DACL" in text:
        return "inherits a descriptor it should not"
    if "DACL" in text:
        return "has an unsafe descriptor"
    return "failed the private-state check"


def inspect_private_state(data_home: Path) -> dict[str, dict[str, str]]:
    """Classify the four private-state locations under one data home.

    Nothing is created. A cold deployment reports four ``absent`` entries, which is a healthy
    answer -- an inspector that turned a missing directory into a fault would make a fresh install
    look broken.
    """
    home = Path(data_home) / "agent-bridge"
    return {
        "data_home": _inspect_directory(home, "the Agent data home").as_dict(),
        STATE_DIR: _inspect_directory(home / "state", "the Bridge state directory").as_dict(),
        LOCK_DIR: _inspect_directory(home / "locks", "the writer lock directory").as_dict(),
        CONFIG_FILE: _inspect_file(home / "bridge.json", "the rendered Bridge config").as_dict(),
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

    if not private_state.WINDOWS:
        # The private-state contract this inspects is the Windows one. On POSIX the equivalent
        # answer requires mode-bit inspection the Bridge does not expose, and guessing would be
        # worse than declining.
        print(
            json.dumps({"error": "private-state inspection is only defined on Windows"}),
            file=sys.stdout,
        )
        return 3

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
