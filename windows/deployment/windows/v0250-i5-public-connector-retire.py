"""Retire ONLY two temporary nonproduction Windows I5 public Connector owners.

This is independent of the public canary: run when I5 / I7 connectors and
I5 Cores/Bridge are Disabled/offline, after remote Cloudflare A/B ingress
has been restored to the original I7 file-only endpoints. Delete only exact
owned I5 Task Scheduler registrations and their disposable copied Tunnel
Tokens (never original I7 credentials). Keep installed venvs and one-use
build dirs for audit; no production, DNS, external API or Provider action.

Do not force-kill processes or delete unknown/foreign manifests. Fail closed
on unexpected active tasks, token identity, task ownership or private ACL.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORES = (
    ("i5-core-a", "i5-edge-a", "i7-edge-a", 18873),
    ("i5-core-b", "i5-edge-b", "i7-edge-b", 18874),
)
PHASE = "entry"
# Native cloudflared owner creates these exact two private files on first run.
# They are retained as auditable, SID-protected evidence after token retirement.
OWNED_ARTIFACTS = frozenset({"service.json", "task.xml", "tunnel.token"})
RUNTIME_EVIDENCE = frozenset({"identity.json", "runtime.log"})


def verify_home_names(names: set[str], *, retired: bool) -> None:
    expected = RUNTIME_EVIDENCE if retired else OWNED_ARTIFACTS | RUNTIME_EVIDENCE
    require(names == expected, "I5 Connector private artifact inventory differs")


class I5PublicRetirementRefusal(RuntimeError):
    """No file paths/keys/command output in failure surface."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5PublicRetirementRefusal(reason)


def helper(name: str, label: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "retirement helper absent")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def native():
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native non-elevated Windows owner required",
    )


def preflight():
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    stage = helper("v0250-i5-bridge-public-connector-stage.py", "i5_retire_stage")
    require(stage.CORES == CORES, "I5 Connector retire scope diverged")
    stage.postflight()
    scheduler = Scheduler()
    manifests = []
    for core, connector, _old_name, port in CORES:
        manifest = storage.load("cloudflare", core, connector)
        require(
            manifest.component == "cloudflare"
            and manifest.instance_id == core
            and manifest.connector_id == connector
            and manifest.settings["hostname"] == core + ".local"
            and manifest.settings["port"] == port,
            "I5 Connector manifest identity drift",
        )
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("state") == "Disabled"
            and state.get("enabled") is False
            and state.get("running") is False
            and _current_identity(manifest) is None,
            "I5 Connector cannot retire while active",
        )
        home = storage.home("cloudflare", core, connector)
        sid = storage._sid()
        verify_home_names({p.name for p in home.iterdir()}, retired=False)
        for name in OWNED_ARTIFACTS | RUNTIME_EVIDENCE:
            storage.check_private(home / name, directory=False, sid=sid)
        manifests.append(manifest)
    print("I5_PUBLIC_TWO_OWNED_CONNECTORS_DISABLED=VERIFIED")
    return stage, tuple(manifests)


def retire():
    global PHASE
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.tasks import Scheduler

    stage, manifests = preflight()
    PHASE = "scoped-remove-two-exact-Scheduler-tasks"
    scheduler = Scheduler()
    for manifest in reversed(manifests):
        response = scheduler.uninstall(manifest)
        require(response.get("registered") is False, "I5 own Task Scheduler deletion not verified")
    print("I5_PUBLIC_TWO_TEMPORARY_TASKS_UNREGISTERED=PASS")
    PHASE = "remove-only-SID-private-copied-nonproduction-artifacts"
    for manifest in reversed(manifests):
        home = storage.home("cloudflare", manifest.instance_id, manifest.connector_id)
        for filename in ("tunnel.token", "task.xml", "service.json"):
            path = home / filename
            storage.check_private(path, directory=False, sid=storage._sid())
            path.unlink()
        verify_home_names({p.name for p in home.iterdir()}, retired=True)
    print("I5_PUBLIC_TWO_COPIED_TUNNEL_TOKENS_RETIRED=PASS")
    PHASE = "final-native-protected-snapshot"
    postflight()
    print("I5_PUBLIC_TEMPORARY_CONNECTOR_RETIRE=PASS")


def postflight():
    from serverfs_mcp.windows_v25 import storage

    native()
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i5_retire_original_i7")
    i5 = helper("v0250-i5-provider-shared-preflight.py", "i5_retire_three_core")
    i7.postflight()
    i5.protected_tasks()
    i5.verify_candidates(i5.candidates(), installed=True)
    require(
        {row["name"] for row in i5.candidates()} == set(i5.TARGETS),
        "unexpected I5 Scheduler identity after retire",
    )
    for core, connector, _old, _port in CORES:
        home = storage.home("cloudflare", core, connector)
        storage.check_private(home, directory=True, sid=storage._sid())
        verify_home_names({p.name for p in home.iterdir()}, retired=True)
        for name in RUNTIME_EVIDENCE:
            storage.check_private(home / name, directory=False, sid=storage._sid())
        require(
            all(
                (home / name).exists() is False and not (home / name).is_symlink()
                for name in ("tunnel.token", "task.xml", "service.json")
            ),
            "retired Connector artifact still exists",
        )
    print("I5_PUBLIC_ORIGINAL_I7_AND_I5_THREE_COMPONENTS_PROTECTED=PASS")
    print("I5_PUBLIC_TEMPORARY_CREDENTIALS_REMOVED=PASS")


def main():
    global PHASE
    p = argparse.ArgumentParser()
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--audit", action="store_true")
    action.add_argument("--retire", action="store_true")
    action.add_argument("--postflight", action="store_true")
    args = p.parse_args()
    PHASE = "native-exact-two-owner-inventory"
    if args.postflight:
        postflight()
        print("I5_PUBLIC_RETIRE_POSTFLIGHT=PASS")
    elif args.audit:
        preflight()
        print("I5_PUBLIC_RETIRE_AUDIT=PASS")
    else:
        retire()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - class-only and bounded phase
        print(
            "I5_PUBLIC_RETIRE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
