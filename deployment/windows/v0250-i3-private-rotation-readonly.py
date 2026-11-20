"""Read-only poststage classifier for installed I3 Core A HMAC rotation.

Does not modify Scheduler, HMAC keys, private bindings, grant documents,
files or processes. Prints only categorical phase markers, never any
credential, key ID, workdir root, PID, SID or raw exception.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path

BRIDGE = "i3-shared"
CORE_A = "i3-core-a"
CORE_B = "i3-core-b"
PHASE = "entry"


class I3AuditError(RuntimeError):
    """Redacted I3 rotation phase classification refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I3AuditError(reason)


def load_rotator():
    path = Path(__file__).with_name("v0250-i3-private-hmac-rotation.py")
    spec = importlib.util.spec_from_file_location("i3_hmac_phase_reader", path)
    require(spec is not None and spec.loader is not None, "I3 rotation source unavailable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    require(
        mod.BRIDGE == BRIDGE and mod.CORE_A == CORE_A and mod.CORE_B == CORE_B,
        "I3 private phase reader identity differs",
    )
    return mod


def classify(grants: dict, core_a: dict, core_b: dict, marker: dict | None) -> str:
    """Accept exactly the four auditable phases, no ambiguous fallback."""
    if not isinstance(grants, dict) or set(grants) != {"schema_version", "bridge_id", "grants"}:
        raise I3AuditError("I3 grants shape invalid")
    if (
        type(grants["schema_version"]) is not int
        or grants["schema_version"] != 1
        or grants["bridge_id"] != BRIDGE
        or not isinstance(grants["grants"], list)
        or len(grants["grants"]) not in (2, 3)
    ):
        raise I3AuditError("I3 grant inventory invalid")
    by_core: dict[str, list[dict]] = {CORE_A: [], CORE_B: []}
    seen: set[tuple[str, str]] = set()
    for grant in grants["grants"]:
        if not isinstance(grant, dict) or set(grant) != {
            "core_id",
            "key_id",
            "secret_hex",
            "methods",
            "workdirs",
        }:
            raise I3AuditError("I3 grant fields invalid")
        core = grant["core_id"]
        if (
            not isinstance(core, str)
            or core not in by_core
            or not isinstance(grant["key_id"], str)
            or (core, grant["key_id"]) in seen
            or not isinstance(grant["methods"], list)
            or len(grant["methods"]) != 2
            or set(grant["methods"]) != {"bridge.ping", "runtime.list"}
            or not isinstance(grant["workdirs"], list)
            or len(grant["workdirs"]) != 1
            or not isinstance(grant["workdirs"][0], dict)
            or set(grant["workdirs"][0]) != {"alias", "policy_sha256"}
            or grant["workdirs"][0]["alias"] != "Fixture"
        ):
            raise I3AuditError("I3 grant scope/identity invalid")
        seen.add((core, grant["key_id"]))
        by_core[core].append(grant)
    if len(by_core[CORE_B]) != 1 or len(by_core[CORE_A]) not in (1, 2):
        raise I3AuditError("I3 grant Core distribution invalid")
    if len(by_core[CORE_A]) + 1 != len(grants["grants"]):
        raise I3AuditError("I3 grant count invalid")

    def matches(binding: dict, grant: dict, expected: str) -> bool:
        return (
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
            and type(binding["schema_version"]) is int
            and binding["schema_version"] == 1
            and binding["instance_id"] == expected
            and binding["bridge_mode"] == "shared"
            and binding["bridge_id"] == BRIDGE
            and binding["key_id"] == grant["key_id"]
            and binding["secret_hex"] == grant["secret_hex"]
            and binding["workdirs"] == grant["workdirs"]
        )

    if not matches(core_b, by_core[CORE_B][0], CORE_B):
        raise I3AuditError("I3 Core B stable grant differs")
    matched_a = [g for g in by_core[CORE_A] if matches(core_a, g, CORE_A)]
    if len(matched_a) != 1:
        raise I3AuditError("I3 Core A binding is not authorized")
    active_a = matched_a[0]
    if marker is None:
        if len(by_core[CORE_A]) != 1:
            raise I3AuditError("I3 unexpected unmarked overlap grant")
        return "READY_ORIGINAL"
    if (
        not isinstance(marker, dict)
        or set(marker) != {"schema_version", "old_core_a_key_id"}
        or type(marker["schema_version"]) is not int
        or marker["schema_version"] != 1
    ):
        raise I3AuditError("I3 one-use marker invalid")
    old_id = marker["old_core_a_key_id"]
    if not isinstance(old_id, str) or not old_id:
        raise I3AuditError("I3 one-use marker key identity invalid")
    grant_ids = [item["key_id"] for item in by_core[CORE_A]]
    if len(grant_ids) == 2:
        if old_id not in grant_ids:
            raise I3AuditError("I3 old identity absent during overlap")
        if active_a["key_id"] == old_id:
            return "OVERLAP_OLD_A_BINDING"
        return "OVERLAP_NEW_A_BINDING"
    if active_a["key_id"] == old_id:
        return "MARKER_ONLY"
    if old_id in grant_ids:
        raise I3AuditError("I3 revoked phase retains old identity")
    return "REVOKED_NEW_A_BINDING"


def main() -> None:
    global PHASE

    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.readiness import _current_identity

    PHASE = "native-owner"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 rotation readonly audit requires native non-admin Python -I -B",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I3 readonly audit requires private Bridge owner",
    )
    rotation = load_rotator()
    helper = rotation.load_helper(
        "v0250-i3-readonly-live-smoke.py", "i3_rotation_audit_native_gate"
    )
    preflight = helper.gate()
    PHASE = "production-and-three-disabled-preflight"
    production_tasks = helper.production_snapshot(preflight)
    incumbent = storage.load("core")
    before = _current_identity(incumbent)
    require(before is not None, "production Core not ready")
    rotation._assert_disabled(helper, preflight)
    print("I3_ROTATION_AUDIT_FIXTURES=DISABLED")

    PHASE = "protected-core-and-bridge-grants"
    sid = storage._sid()
    bridge = storage.load("bridge", BRIDGE)
    load_native_bridge_plan(bridge)
    home = storage.home("bridge", BRIDGE)
    grant_doc = rotation.read_private_json(home / "state" / "core-grants.json", sid)
    a = rotation.read_private_json(
        storage.home("core", CORE_A) / "bridge-binding.json", sid, maximum=8192
    )
    b = rotation.read_private_json(
        storage.home("core", CORE_B) / "bridge-binding.json", sid, maximum=8192
    )
    marker_path = home / "state" / ".i3-hmac-core-a-rotation-v1.attempted"
    if marker_path.exists() or marker_path.is_symlink():
        marker = rotation.read_private_json(marker_path, sid, maximum=4096)
    else:
        marker = None
    state = classify(grant_doc, a, b, marker)
    print("I3_ROTATION_AUDIT_STATE=" + state)
    PHASE = "production-postflight"
    require(
        helper.production_snapshot(preflight) == production_tasks
        and _current_identity(incumbent) == before,
        "production Scheduler or Core changed",
    )
    print("I3_ROTATION_AUDIT_PRODUCTION=UNCHANGED")
    print("I3_ROTATION_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed sanitized phase/class
        print(
            "I3_ROTATION_READONLY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
