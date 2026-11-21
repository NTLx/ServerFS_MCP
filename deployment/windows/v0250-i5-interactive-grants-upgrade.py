"""I5 nonproduction interactive-method grant correction, Windows native and one-use.

Only the two existing Core grants of the disabled/offline I5 shared Bridge may
gain task.approval.respond and task.question.answer. Never rotates a key,
changes Core bindings, starts services, or reads/writes production credentials.
An exact, SID-private backup + durable attempt marker precede the atomic write.
Source-only --audit and --postflight never modify private state.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

BRIDGE = "i5-shared"
CORES = ("i5-core-a", "i5-core-b")
OLD_METHODS = (
    "bridge.ping",
    "runtime.list",
    "task.submit",
    "task.get",
    "task.events",
    "task.result.read",
    "task.cancel",
)
ADDED_METHODS = ("task.approval.respond", "task.question.answer")
NEW_METHODS = OLD_METHODS + ADDED_METHODS
MARKER = ".i5-interactive-grants-v1.attempted.json"
BACKUP = ".i5-interactive-grants-v1.old.json"
PHASE = "entry"


class I5GrantUpgradeError(RuntimeError):
    """Never return grant bytes, private identities, secrets or paths."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I5GrantUpgradeError(reason)


def helper(filename: str, label: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed I5 helper absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(grants: dict, bindings: dict[str, dict], methods: tuple[str, ...]) -> None:
    """Pure exact-grant+Core-binding match; no secret ever returned or printed."""
    require(
        isinstance(grants, dict)
        and set(grants) == {"schema_version", "bridge_id", "grants"}
        and type(grants["schema_version"]) is int
        and grants["schema_version"] == 1
        and grants["bridge_id"] == BRIDGE
        and type(grants["grants"]) is list
        and len(grants["grants"]) == 2
        and set(bindings) == set(CORES),
        "I5 owner grant inventory mismatch",
    )
    entries = {}
    for item in grants["grants"]:
        require(
            isinstance(item, dict)
            and set(item) == {"core_id", "key_id", "secret_hex", "methods", "workdirs"},
            "I5 grant shape differs",
        )
        core = item["core_id"]
        require(
            isinstance(core, str) and core in CORES and core not in entries,
            "I5 Core identities drift",
        )
        require(
            type(item["methods"]) is list
            and len(item["methods"]) == len(methods)
            and set(item["methods"]) == set(methods),
            "I5 grant method scope drift",
        )
        entries[core] = item
    require(set(entries) == set(CORES), "I5 signed Core grants incomplete")
    for core, item in entries.items():
        binding = bindings[core]
        require(
            isinstance(binding, dict)
            and set(binding)
            == {
                "schema_version",
                "instance_id",
                "bridge_mode",
                "bridge_id",
                "key_id",
                "secret_hex",
                "workdirs",
            }
            and binding["schema_version"] == 1
            and binding["instance_id"] == core
            and binding["bridge_mode"] == "shared"
            and binding["bridge_id"] == BRIDGE
            and item["key_id"] == binding["key_id"]
            and item["secret_hex"] == binding["secret_hex"]
            and isinstance(item["secret_hex"], str)
            and len(item["secret_hex"]) == 64
            and all(ch in "0123456789abcdef" for ch in item["secret_hex"])
            and item["workdirs"] == binding["workdirs"]
            and type(item["workdirs"]) is list
            and len(item["workdirs"]) == 1
            and set(item["workdirs"][0]) == {"alias", "policy_sha256"}
            and item["workdirs"][0]["alias"] == "Fixture",
            "I5 Core grant/binding equality refused",
        )
    require(
        entries[CORES[0]]["key_id"] != entries[CORES[1]]["key_id"]
        and entries[CORES[0]]["secret_hex"] != entries[CORES[1]]["secret_hex"],
        "I5 two Core keys are not independent",
    )


def context():
    """Authoritative Windows current-user, protected I5 private inventory."""
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
        and current_user_sid() == storage._sid(),
        "native current-user Windows -I -B required",
    )
    stage = helper("v0250-i5-provider-shared-stage.py", "i5_grant_source_identity")
    smoke = helper("v0250-i5-provider-installed-smoke.py", "i5_grant_native_gate")
    gate = smoke.load_gate()
    require(
        stage.BRIDGE == BRIDGE
        and tuple(stage.CORE_PORTS) == CORES
        and stage.METHODS == NEW_METHODS,
        "I5 source method contract differs",
    )
    baseline = smoke.protected_snapshot(gate)
    smoke.candidates_disabled(gate)
    manifests = [
        storage.load("bridge", BRIDGE),
        *(storage.load("core", core) for core in CORES),
    ]
    for manifest in manifests:
        status = Scheduler().status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("state") == "Disabled"
            and status.get("running") is False
            and status.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I5 fixture Owner not Disabled/offline",
        )
    production = storage.load("core")
    birth = _current_identity(production)
    require(birth is not None, "protected production Core unavailable")

    sid = storage._sid()
    home = storage.home("bridge", BRIDGE)
    state = home / "state"
    storage.check_private(state, directory=True, sid=sid)
    load_native_bridge_plan(manifests[0])

    reader = helper("v0250-i3-private-hmac-rotation.py", "i5_private_json_reader")
    grants_path = state / "core-grants.json"
    current = reader.read_private_json(grants_path, sid)
    bindings = {}
    for core in CORES:
        bindings[core] = reader.read_private_json(
            storage.home("core", core) / "bridge-binding.json", sid, maximum=8192
        )
    return (
        stage,
        smoke,
        gate,
        baseline,
        manifests,
        production,
        birth,
        current,
        bindings,
        reader,
        state,
        grants_path,
        sid,
    )


def protected_after(smoke, gate, baseline, production, birth) -> None:
    from serverfs_mcp.windows_v25.readiness import _current_identity

    smoke.candidates_disabled(gate)
    require(smoke.protected_snapshot(gate) == baseline, "protected Scheduler drift")
    after = _current_identity(production)
    require(
        after is not None and after.pid == birth.pid and after.creation_time == birth.creation_time,
        "production Core generation changed",
    )
    print("I5_INTERACTIVE_GRANT_PRODUCTION_AND_FIXTURES=UNCHANGED")


def run(action: str) -> None:
    global PHASE
    (
        stage,
        smoke,
        gate,
        baseline,
        manifests,
        production,
        birth,
        current,
        bindings,
        reader,
        state,
        grants_path,
        sid,
    ) = context()
    marker = state / MARKER
    backup = state / BACKUP
    from serverfs_mcp.windows_v25 import storage

    if action == "audit":
        PHASE = "legacy-private-method-inventory"
        require(
            not marker.exists()
            and not marker.is_symlink()
            and not backup.exists()
            and not backup.is_symlink(),
            "I5 interactive grant upgrade already attempted",
        )
        validate(current, bindings, OLD_METHODS)
        print("I5_INTERACTIVE_GRANTS_MISSING_TWO_METHODS=VERIFIED")
        protected_after(smoke, gate, baseline, production, birth)
        print("I5_INTERACTIVE_GRANT_READONLY_AUDIT=PASS")
        return

    if action == "postflight":
        PHASE = "private-upgrade-postflight"
        reader.read_private_json(marker, sid, maximum=1024)
        historical = reader.read_private_json(backup, sid)
        validate(historical, bindings, OLD_METHODS)
        validate(current, bindings, NEW_METHODS)
        for original, updated in zip(historical["grants"], current["grants"], strict=True):
            require(
                {k: v for k, v in original.items() if k != "methods"}
                == {k: v for k, v in updated.items() if k != "methods"},
                "I5 private grant non-method field modified",
            )
        protected_after(smoke, gate, baseline, production, birth)
        print("I5_INTERACTIVE_GRANTS_TWO_METHODS_ONLY=PASS")
        print("I5_INTERACTIVE_GRANT_READONLY_POSTFLIGHT=PASS")
        return

    PHASE = "one-use-legacy-preflight"
    require(
        action == "apply-once"
        and not marker.exists()
        and not marker.is_symlink()
        and not backup.exists()
        and not backup.is_symlink(),
        "I5 grant upgrade one-use marker occupied",
    )
    validate(current, bindings, OLD_METHODS)
    PHASE = "create-private-attempt-marker-and-backup"
    i2 = helper("v0250-i2-stage.py", "i5_grant_private_writer")
    i3 = helper("v0250-i3-readonly-stage.py", "i5_grant_worker_provisioner")
    i2._new_private_file(marker, stage._json({"schema_version": 1, "bridge_id": BRIDGE}), sid)
    i2._new_private_file(backup, stage._json(current), sid)
    storage.check_private(backup, directory=False, sid=sid)
    require(reader.read_private_json(backup, sid) == current, "I5 backup mismatch")

    PHASE = "sealed-owner-only-atomic-grant-provision"
    upgraded = {
        **current,
        "grants": [{**item, "methods": list(NEW_METHODS)} for item in current["grants"]],
    }
    validate(upgraded, bindings, NEW_METHODS)
    worker = storage.root(BRIDGE) / "envs/agent-bridge/Scripts/python.exe"
    config = storage.home("bridge", BRIDGE) / "bridge.json"
    storage.check_private(worker, directory=False, sid=sid)
    storage.check_private(config, directory=False, sid=sid)
    response = i2.run(
        [str(worker), "-I", "-B", "-c", i3._PROVISION, str(config)],
        input_data=stage._json(upgraded),
        timeout=45,
    )
    require(
        response.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
        "I5 owner-only worker grant provisioning refused",
    )
    PHASE = "independent-source-and-owner-postflight"
    after = reader.read_private_json(grants_path, sid)
    require(after == upgraded, "I5 post-provision grant mismatch")
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan

    load_native_bridge_plan(manifests[0])
    protected_after(smoke, gate, baseline, production, birth)
    print("I5_INTERACTIVE_GRANTS_TWO_METHODS_ONLY=PASS")
    print("I5_INTERACTIVE_GRANT_ONE_USE_UPGRADE=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--audit", action="store_true")
    choice.add_argument("--apply-once", action="store_true")
    choice.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    run("audit" if args.audit else "apply-once" if args.apply_once else "postflight")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 — private data must not escape
        print(
            "I5_INTERACTIVE_GRANT=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
