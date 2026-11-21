"""I14: exact scoped retirement for two temporary I13 Cloudflare Connectors.

Requires INDEPENDENT VERIFIED remote Cloudflare restore to the original I7
nonproduction ingress (127.0.0.1:18875, i7-public.local, 404 fallback).
This program cannot assert remote restoration; the explicit operator
precondition is mandatory. Does not control any remote Cloudflare resource.
Only unregisters the two I14 current-user Disabled tasks and unlinks their
own copied Tokens/task XML/manifest; preserves private runtime evidence.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

ROUTES = (("i13-a", "i14-edge-a"), ("i13-b", "i14-edge-b"))
OWNED = frozenset({"service.json", "task.xml", "tunnel.token"})
EVIDENCE = frozenset({"identity.json", "runtime.log"})
PHASE = "entry"


class I14RetireRefusal(RuntimeError):
    """A bounded refusal without secrets, local absolute paths or raw OS errors."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I14RetireRefusal(message)


def helper(filename: str, label: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "trusted I14 helper missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def native() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "I14 retirement requires native ordinary SID -I -B",
    )


def inventory(names: set[str], *, retired: bool) -> None:
    require(
        names == (EVIDENCE if retired else OWNED | EVIDENCE),
        "I14 private Connector artifact inventory not exact",
    )


def audit():
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    stage = helper("v0250-i14-two-owned-repo-public-connector-stage.py", "i14_retire_stage")
    require(
        tuple((core, conn) for core, conn, _old, _port, _host in stage.ROUTES) == ROUTES,
        "I14 temporary Connector identities drifted",
    )
    stage.postflight()
    scheduler = Scheduler()
    manifests = []
    for core, connector in ROUTES:
        manifest = storage.load("cloudflare", core, connector)
        status = scheduler.status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("running") is False
            and status.get("state") == "Disabled"
            and _current_identity(manifest) is None,
            "test Connector must be offline before retirement",
        )
        home = storage.home("cloudflare", core, connector)
        inventory({item.name for item in home.iterdir()}, retired=False)
        for name in OWNED | EVIDENCE:
            storage.check_private(home / name, directory=False, sid=storage._sid())
        manifests.append(manifest)
    print("I14_TWO_NATIVE_TEMPORARY_CONNECTORS_DISABLED=VERIFIED")
    return tuple(manifests)


def retire() -> None:
    global PHASE
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.tasks import Scheduler

    manifests = audit()
    PHASE = "unregister-only-two-exact-i14-Disabled-tasks"
    scheduler = Scheduler()
    for manifest in reversed(manifests):
        result = scheduler.uninstall(manifest)
        require(result.get("registered") is False, "scoped Task unregister unverified")
    print("I14_TWO_TEMPORARY_SCHEDULER_TASKS_UNREGISTERED=PASS")
    PHASE = "remove-only-two-copied-private-Tokens-and-manifests"
    for manifest in reversed(manifests):
        home = storage.home("cloudflare", manifest.instance_id, manifest.connector_id)
        for name in ("tunnel.token", "task.xml", "service.json"):
            file = home / name
            storage.check_private(file, directory=False, sid=storage._sid())
            file.unlink()
        inventory({item.name for item in home.iterdir()}, retired=True)
    print("I14_TWO_COPIED_TEST_ONLY_TOKENS_REMOVED=PASS")
    PHASE = "independent-I13-I7-production-protected-postflight"
    postflight()
    print("I14_TEMPORARY_TWO_CONNECTORS_RETIRED=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    native()
    stage = helper("v0250-i14-two-owned-repo-public-connector-stage.py", "i14_retired_stage")
    _i13, _i7, pre, _owner, _binary = stage.baseline()
    require(
        {item["name"] for item in pre.task_rows()} == pre.TASK_NAMES,
        "I14 temporary Scheduler identities still registered",
    )
    for core, connector in ROUTES:
        home = storage.home("cloudflare", core, connector)
        storage.check_private(home, directory=True, sid=storage._sid())
        inventory({item.name for item in home.iterdir()}, retired=True)
        for name in EVIDENCE:
            storage.check_private(home / name, directory=False, sid=storage._sid())
        for name in OWNED:
            path = home / name
            require(
                not path.exists() and not path.is_symlink(),
                "scoped temporary credential/manifest remains",
            )
    print("I14_ONLY_SID_PRIVATE_RUNTIME_EVIDENCE_RETAINED=PASS")
    print("I14_I13_I7_PRODUCTION_UNCHANGED=PASS")


def main() -> None:
    global PHASE
    p = argparse.ArgumentParser()
    exclusive = p.add_mutually_exclusive_group(required=True)
    exclusive.add_argument("--audit", action="store_true")
    exclusive.add_argument("--retire-after-verified-remote-restore", action="store_true")
    exclusive.add_argument("--postflight", action="store_true")
    flags = p.parse_args()
    if flags.audit:
        audit()
        print("I14_PRIVATE_NATIVE_RETIRE_AUDIT=PASS")
    elif flags.retire_after_verified_remote_restore:
        retire()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - bounded phase+class only
        print(
            "I14_CONNECTOR_RETIRE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
