"""I10 native Windows cleanup of exact two temporary Connector owners only.

Precondition: operator independently restored BOTH nonproduction Cloudflare
Tunnel A/B ingress routes to original I7 host/port, verified remote readback,
then this scoped retirement may be called. Remote DNS/Tunnel is never changed
here. Never delete original I7 Tokens, I5 Core/Bridge, or other instances.
The private runtime identity/log evidence is retained. Strict inventory.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

CORE = "i5-core-a"
IDS = ("i10-edge-a", "i10-edge-b")
OWNED = frozenset({"service.json", "task.xml", "tunnel.token"})
EVIDENCE = frozenset({"identity.json", "runtime.log"})
PHASE = "entry"


class I10RetireRefusal(RuntimeError):
    """Do not disclose private identifiers or credential data."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I10RetireRefusal(reason)


def helper(filename: str, name: str):
    src = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, src)
    require(spec is not None and spec.loader is not None, "fixed helper absent")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def exact_names(names: set[str], *, retired: bool) -> None:
    expected = EVIDENCE if retired else OWNED | EVIDENCE
    require(names == expected, "I10 private Owner artifact inventory differs")


def native():
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I10 retirement requires native ordinary-user Windows -I -B",
    )


def audit():
    """Never mutates; requires exact test-owned tasks inactive and artifacts."""
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    stage = helper("v0250-i10-same-core-dual-connectors-stage.py", "i10_retire_stage")
    require(
        stage.CORE == CORE and tuple(item[0] for item in stage.CONNECTORS) == IDS,
        "I10 retirement scope differs",
    )
    stage.postflight()
    manifests = []
    scheduler = Scheduler()
    for connector in IDS:
        manifest = storage.load("cloudflare", CORE, connector)
        require(
            manifest.component == "cloudflare"
            and manifest.instance_id == CORE
            and manifest.connector_id == connector,
            "I10 private Connector manifest identity drift",
        )
        status = scheduler.status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("state") == "Disabled"
            and status.get("running") is False
            and _current_identity(manifest) is None,
            "I10 Connector still active",
        )
        home = storage.home("cloudflare", CORE, connector)
        exact_names({entry.name for entry in home.iterdir()}, retired=False)
        for name in OWNED | EVIDENCE:
            storage.check_private(home / name, directory=False, sid=storage._sid())
        manifests.append(manifest)
    print("I10_TWO_TEMPORARY_CONNECTOR_OWNERS_OFFLINE=VERIFIED")
    return tuple(manifests)


def retire() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.tasks import Scheduler

    manifests = audit()
    PHASE = "remove-only-two-exact-I10-Scheduler-entries"
    scheduler = Scheduler()
    for manifest in reversed(manifests):
        outcome = scheduler.uninstall(manifest)
        require(outcome.get("registered") is False, "I10 task retirement not verified")
    print("I10_TWO_TEMPORARY_TASKS_UNREGISTERED=PASS")
    PHASE = "remove-only-two-copied-nonproduction-Tokens-and-owned-manifests"
    for manifest in reversed(manifests):
        home = storage.home("cloudflare", CORE, manifest.connector_id)
        for name in ("tunnel.token", "task.xml", "service.json"):
            path = home / name
            storage.check_private(path, directory=False, sid=storage._sid())
            path.unlink()
        exact_names({entry.name for entry in home.iterdir()}, retired=True)
    print("I10_TWO_COPIED_NONPROD_TOKENS_RETIRED=PASS")
    PHASE = "independent-source-and-native-protected-postflight"
    postflight()
    print("I10_TEMPORARY_CONNECTOR_RETIRE=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    native()
    old = helper("v0250-i5-public-connector-retire.py", "i10_retired_i5_pair")
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i10_original_i7")
    i5 = helper("v0250-i5-provider-shared-preflight.py", "i10_protected_i5")
    old.postflight()
    i7.postflight()
    i5.protected_tasks()
    candidates = i5.candidates()
    i5.verify_candidates(candidates, installed=True)
    require(
        set(row["name"] for row in candidates) == set(i5.TARGETS),
        "I10 Scheduler entries remain after retirement",
    )
    for connector in IDS:
        home = storage.home("cloudflare", CORE, connector)
        storage.check_private(home, directory=True, sid=storage._sid())
        exact_names({entry.name for entry in home.iterdir()}, retired=True)
        for name in EVIDENCE:
            storage.check_private(home / name, directory=False, sid=storage._sid())
        for name in OWNED:
            path = home / name
            require(
                not path.exists() and not path.is_symlink(),
                "I10 copied credential/manifest artifact remains",
            )
    print("I10_ONLY_NATIVE_RUNTIME_EVIDENCE_RETAINED=PASS")
    print("I10_I7_I5_PRODUCTION_PROTECTED_POST_RETIRE=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    option = parser.add_mutually_exclusive_group(required=True)
    option.add_argument("--audit", action="store_true")
    option.add_argument("--retire-after-remote-restore", action="store_true")
    option.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    PHASE = "native-disposable-I10-private-Owner-inventory"
    if args.audit:
        audit()
        print("I10_NATIVE_RETIRE_AUDIT=PASS")
    elif args.retire_after_remote_restore:
        retire()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - stable phase/type only
        print("I10_RETIRE=FAILED phase=" + PHASE + " type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
