"""Read-only I5 protection-gate diagnosis using the proven I4 task query.

Do not mutate Task Scheduler, read task actions/XML, inspect secrets, or
change the protection baseline. Output is only fixed expected/actual
state + enable flags for the existing 14 named protected services.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path


class ProtectedInventoryError(RuntimeError):
    """Sanitized task inventory failure."""


def load_i4_gate():
    source = Path(__file__).with_name("v0250-i4-independent-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_protected_task_inventory", source)
    if spec is None or spec.loader is None:
        raise ProtectedInventoryError("I4 task inventory code unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mismatches(gate, rows: object) -> list[tuple[str, str, bool, str, object]]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ProtectedInventoryError("protected inventory malformed")
    expected = {
        **gate.PRODUCTION,
        **{name: (False, "Disabled") for name in (*gate.EXISTING_FIXTURES, *gate.TARGETS)},
    }
    if len(expected) != 14:
        raise ProtectedInventoryError("protected inventory identity count differs")
    result = []
    for name, (want_enabled, want_state) in expected.items():
        found = [row for row in rows if row.get("name") == name]
        if len(found) != 1:
            result.append((name, want_state, want_enabled, "MISSING_OR_DUPLICATE", "UNKNOWN"))
            continue
        row = found[0]
        actual_state, actual_enabled = row.get("state"), row.get("enabled")
        if actual_state != want_state or actual_enabled is not want_enabled:
            result.append((name, want_state, want_enabled, str(actual_state), actual_enabled))
    return result


def main() -> None:
    if (
        sys.platform != "win32"
        or os.name != "nt"
        or not sys.flags.isolated
        or not sys.dont_write_bytecode
        or ctypes.windll.shell32.IsUserAnAdmin()
    ):
        raise ProtectedInventoryError("native non-elevated Python -I -B required")
    gate = load_i4_gate()
    rows = gate.scheduler_rows()
    differences = mismatches(gate, rows)
    print("I5_PROTECTED_TASKS_QUERIED=" + str(len(rows)))
    if differences:
        for name, expected_state, expected_enabled, actual_state, actual_enabled in differences:
            print(
                "I5_PROTECTED_DIFF="
                + name
                + " expected="
                + expected_state
                + "/"
                + str(expected_enabled)
                + " actual="
                + actual_state
                + "/"
                + str(actual_enabled)
            )
        print("I5_PROTECTED_TASK_DIFF=DETECTED")
    else:
        gate.verify_task_rows(rows, require_candidates_absent=False)
        print("I5_PROTECTED_TASK_DIFF=NONE")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no protected path or command text
        print("I5_PROTECTED_TASK_DIFF=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
