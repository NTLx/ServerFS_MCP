"""I14: short, strictly read-only Windows recovery snapshot after one-use stage.

Does NOT run stage/audit/postflight, inspect Token bytes or start/stop tasks.
Avoids relying on Claude Bash background TaskOutput, which is session-local.
A COMPLETE_LAYOUT (Disabled-only) result needs a separate installed postflight;
it does not attest historical stage process exit or Cloudflare public ingress.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

PHASE = "entry"
TASKS = (
    ("i13-a", "i14-edge-a"),
    ("i13-b", "i14-edge-b"),
)
FIELDS = (
    "HOME",
    "MANIFEST",
    "TASK_XML",
    "TOKEN_PRESENT",
    "IDENTITY",
    "RUNTIME_LOG",
    "PYTHON",
    "PYTHONW",
)


class I14StateRefusal(RuntimeError):
    """Stable nonsecret refusal with no paths, tokens, PIDs or scheduler XML."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I14StateRefusal(message)


def helper(filename: str, label: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "trusted I14 helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify(
    rows: list[dict],
    *,
    core_names: frozenset[str],
    connectors: frozenset[str],
    exists: tuple[bool, ...],
) -> str:
    """Pure, fail-closed three-state decision from one bounded snapshot."""
    require(
        len(exists) == 16 and all(type(value) is bool for value in exists),
        "private installation snapshot malformed",
    )
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) and type(row.get("name")) is str for row in rows)
        and len({row["name"] for row in rows}) == len(rows),
        "Scheduler inventory malformed or duplicated",
    )
    names = {row["name"] for row in rows}
    require(
        core_names <= names and names <= core_names | connectors,
        "unexpected native Task identity",
    )
    require(
        all(row.get("state") == "Disabled" and row.get("enabled") is False for row in rows),
        "unregistered or running native owner ambiguity",
    )
    if names == core_names and not any(exists):
        return "FRESH"
    # identity.json/runtime.log are created by the component runtime, not by
    # a Disabled-only install. Requiring them here creates a false failure.
    if names == core_names | connectors and all(
        present for index, present in enumerate(exists) if index % 8 not in (4, 5)
    ):
        return "COMPLETE_LAYOUT"
    return "PARTIAL_OR_IN_PROGRESS"


def snapshot(*, details: bool = False) -> str:
    global PHASE
    from serverfs_mcp.windows_v25 import storage

    PHASE = "ordinary-Windows-private-owner-gate"
    native = helper("v0250-i14-two-owned-repo-public-connector-stage.py", "i14_snapshot_native")
    native.native()
    pre = helper("v0250-i13-owned-repo-preflight.py", "i14_snapshot_task_allowlist")
    require(
        TASKS == (("i13-a", "i14-edge-a"), ("i13-b", "i14-edge-b")),
        "exact I14 pairs drifted",
    )
    PHASE = "read-only-exact-Scheduler-and-private-artifact-inventory"
    rows = pre.task_rows()
    if details:
        names = {row["name"] for row in rows}
        print("I14_EXACT_I13_OWNER_TASK_COUNT=" + str(len(names & pre.TASK_NAMES)))
        print("I14_EXACT_CONNECTOR_TASK_COUNT=" + str(len(names & pre.OPTIONAL_CONNECTOR_NAMES)))
    observed: list[bool] = []
    for core, connector in TASKS:
        base = storage.root(core)
        home = storage.home("cloudflare", core, connector)
        for label, item in zip(
            FIELDS,
            (
                home,
                home / "service.json",
                home / "task.xml",
                home / "tunnel.token",
                home / "identity.json",
                home / "runtime.log",
                base / "envs/cloudflare/Scripts/python.exe",
                base / "envs/cloudflare/Scripts/pythonw.exe",
            ),
            strict=True,
        ):
            # Never follow symlinks, read or emit private credentials or host paths.
            require(not item.is_symlink(), "private artifact path redirected")
            present = item.exists()
            observed.append(present)
            if details:
                print(
                    "I14_"
                    + core.upper().replace("-", "_")
                    + "_"
                    + label
                    + "="
                    + ("PRESENT" if present else "ABSENT")
                )
    result = classify(
        rows,
        core_names=pre.TASK_NAMES,
        connectors=pre.OPTIONAL_CONNECTOR_NAMES,
        exists=tuple(observed),
    )
    print("I14_READONLY_LAYOUT_STATE=" + result)
    return result


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", action="store_true", required=True)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()
    require(args.snapshot, "explicit read-only snapshot required")
    result = snapshot(details=args.details)
    require(result != "PARTIAL_OR_IN_PROGRESS", "I14 one-use stage incomplete/uncertain")
    print("I14_READONLY_STATE_SNAPSHOT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - bounded class and phase only
        print(
            "I14_READONLY_STATE_SNAPSHOT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
