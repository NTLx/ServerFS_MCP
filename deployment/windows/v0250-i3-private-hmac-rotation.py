"""I3 private Core A HMAC key rotation, scoped only to installed read-only I3.

This is a *one-use* owner operation. It requires exactly two original grants
and the three I3 tasks Disabled. Rotation is monotonic:

  (A) Bridge grants: old A + NEW A + B (overlap, old binding still valid)
  (B) Prove overlap using the real signed Bridge; stop it.
  (C) Atomically replace ONLY Core A private binding with NEW A.
  (D) Bridge grants: NEW A + B (old A revoked), restart Bridge and prove.

A failure preserves the private phase for a separate read-only diagnostic;
NEVER blindly rerun this original-state-only rotator. Other tasks, Core B
binding, production credentials, public tunnels and Agent providers are not
operated. HMAC records are never printed or placed in exceptions.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path

BRIDGE = "i3-shared"
CORE_A = "i3-core-a"
CORE_B = "i3-core-b"
EXPECTED_METHODS = frozenset({"bridge.ping", "runtime.list"})
PHASE = "entry"


class I3RotateError(RuntimeError):
    """Redacted, one-use private operator rotation failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise I3RotateError(message)


def load_helper(name: str, label: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "fixed I3 helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unique_pairs(items: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in items:
        require(key not in result, "private I3 document has ambiguous fields")
        result[key] = value
    return result


def read_private_json(path: Path, sid: str, *, maximum: int = 131072) -> dict:
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(path, directory=False, sid=sid)
    info = path.stat()
    require(
        2 <= info.st_size <= maximum and info.st_nlink == 1,
        "private I3 document size/link protection differs",
    )
    document = json.loads(path.read_text("utf-8"), object_pairs_hook=unique_pairs)
    require(isinstance(document, dict), "private I3 document has invalid shape")
    return document


def _record_map(grants: dict) -> dict[str, dict]:
    require(
        set(grants) == {"schema_version", "bridge_id", "grants"}
        and type(grants["schema_version"]) is int
        and grants["schema_version"] == 1
        and grants["bridge_id"] == BRIDGE
        and isinstance(grants["grants"], list)
        and len(grants["grants"]) == 2,
        "I3 original grant inventory is not exactly two",
    )
    by_core: dict[str, dict] = {}
    for entry in grants["grants"]:
        require(
            isinstance(entry, dict)
            and set(entry) == {"core_id", "key_id", "secret_hex", "methods", "workdirs"},
            "I3 grant shape differs",
        )
        core = entry["core_id"]
        require(
            isinstance(core, str) and core in (CORE_A, CORE_B) and core not in by_core,
            "I3 grant Core owner differs",
        )
        require(
            isinstance(entry["methods"], list)
            and set(entry["methods"]) == EXPECTED_METHODS
            and len(entry["methods"]) == 2,
            "I3 grant methods exceed read-only health",
        )
        require(
            isinstance(entry["workdirs"], list)
            and len(entry["workdirs"]) == 1
            and isinstance(entry["workdirs"][0], dict)
            and set(entry["workdirs"][0]) == {"alias", "policy_sha256"}
            and entry["workdirs"][0]["alias"] == "Fixture",
            "I3 grant workdir scope differs",
        )
        by_core[core] = entry
    require(set(by_core) == {CORE_A, CORE_B}, "I3 original grant Core identities differ")
    return by_core


def _binding_grant_consistent(binding: dict, grant: dict, *, core_id: str) -> bool:
    return (
        set(binding)
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
        and binding["instance_id"] == core_id
        and binding["bridge_mode"] == "shared"
        and binding["bridge_id"] == BRIDGE
        and binding["key_id"] == grant["key_id"]
        and binding["secret_hex"] == grant["secret_hex"]
        and binding["workdirs"] == grant["workdirs"]
    )


def _sealed_documents(grants: dict, core_a: dict, core_b: dict) -> tuple[dict, dict]:
    """Strict original-state contract; never accepts half-completed rotations."""
    by_core = _record_map(grants)
    require(
        _binding_grant_consistent(core_a, by_core[CORE_A], core_id=CORE_A)
        and _binding_grant_consistent(core_b, by_core[CORE_B], core_id=CORE_B),
        "I3 Core binding and Bridge grants differ",
    )
    require(
        by_core[CORE_A]["key_id"] != by_core[CORE_B]["key_id"]
        and by_core[CORE_A]["secret_hex"] != by_core[CORE_B]["secret_hex"],
        "I3 Core keys are not independent",
    )
    return by_core[CORE_A], by_core[CORE_B]


def _assert_disabled(probe, preflight) -> None:
    probe.three_task_states(preflight)
    for component, instance in (("bridge", BRIDGE), ("core", CORE_A), ("core", CORE_B)):
        status = probe.cli(component, instance, "status")
        require(
            status.get("state") == "Disabled"
            and status.get("running") is False
            and status.get("enabled") is False
            and status.get("readiness") == "offline",
            "I3 fixture is not Disabled/offline",
        )


def _private_replace(path: Path, payload: bytes, sid: str) -> None:
    """One private same-directory temp, strict DACL and atomic Windows replacement."""
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    home = path.parent
    storage.check_private(home, directory=True, sid=sid)
    storage.check_private(path, directory=False, sid=sid)
    require(path.stat().st_nlink == 1 and not path.is_symlink(), "I3 private binding changed")
    temp = home / (".bridge-binding-" + secrets.token_hex(16) + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(temp, sid)
        storage.check_private(temp, directory=False, sid=sid)
        require(temp.stat().st_nlink == 1, "I3 private staging has multiple links")
        # Scope exactly one Core A binding: the parent and target are private.
        os.replace(temp, path)
        storage.check_private(path, directory=False, sid=sid)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    global PHASE

    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.core_bridge_binding import (
        load_native_core_bridge_binding,
        parse_core_bridge_binding,
    )
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.shared_core_grants import issue_shared_core_capabilities

    PHASE = "native-private-owner"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 private rotation needs non-admin Python -I -B",
    )
    expected = storage.root(BRIDGE) / "envs" / "bridge" / "Scripts" / "python.exe"
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I3 private rotation must use installed Bridge owner",
    )
    probe = load_helper("v0250-i3-readonly-live-smoke.py", "i3_rotate_native_lifecycle")
    stage = load_helper("v0250-i3-readonly-stage.py", "i3_rotate_source_helpers")
    preflight = probe.gate()
    sid = storage._sid()
    PHASE = "preflight-no-service-running"
    baseline = probe.production_snapshot(preflight)
    production = storage.load("core")
    production_before = _current_identity(production)
    require(production_before is not None, "production Core generation unavailable")
    _assert_disabled(probe, preflight)
    bridge = storage.load("bridge", BRIDGE)
    load_native_bridge_plan(bridge)
    owner_home = storage.home("bridge", BRIDGE)
    grants_path = owner_home / "state" / "core-grants.json"
    original_grants = read_private_json(grants_path, sid)

    core = {}
    original_bindings = {}
    clients = {}
    for core_id in (CORE_A, CORE_B):
        core[core_id] = storage.load("core", core_id)
        workdirs, settings = load_native_config(Path(str(core[core_id].settings["config"])))
        require(settings.bridge_required and len(workdirs) == 1, "I3 Core policy differs")
        original_bindings[core_id] = read_private_json(
            storage.home("core", core_id) / "bridge-binding.json", sid, maximum=8192
        )
        clients[core_id] = load_native_core_bridge_binding(core[core_id], workdirs)
    old_a, old_b = _sealed_documents(
        original_grants, original_bindings[CORE_A], original_bindings[CORE_B]
    )
    digest = old_a["workdirs"][0]["policy_sha256"]
    require(
        old_b["workdirs"][0]["policy_sha256"] == digest, "I3 Core bridge policy revisions differ"
    )

    PHASE = "generate-new-private-capability"
    (new_cap,) = issue_shared_core_capabilities(
        bridge_id=BRIDGE,
        core_ids=(CORE_A,),
        workdir_policy_revisions={"Fixture": digest},
        methods=("bridge.ping", "runtime.list"),
    )
    new_a_grant = new_cap.bridge_grant()
    new_a_binding = new_cap.core_binding()
    require(
        new_a_grant["key_id"] not in {old_a["key_id"], old_b["key_id"]}
        and new_a_grant["secret_hex"] not in {old_a["secret_hex"], old_b["secret_hex"]},
        "I3 new private Core key collided",
    )
    parsed_new = parse_core_bridge_binding(
        core[CORE_A],
        new_a_binding,
        local_aliases=frozenset({"Fixture"}),
        verified_bridge=bridge,
        bridge_lock_dir=owner_home / "locks",
    )
    worker = storage.root(BRIDGE) / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    storage.check_private(worker, directory=False, sid=sid)
    config = owner_home / "bridge.json"
    storage.check_private(config, directory=False, sid=sid)

    overlap_document = {
        "schema_version": 1,
        "bridge_id": BRIDGE,
        "grants": [old_a, new_a_grant, old_b],
    }
    private_helper = stage.load_helper("v0250-i2-stage.py", "i3_rotate_private_worker_helper")
    # Persist the one-shot guard BEFORE changing grants. Any crash/partial
    # completion requires a separate read-only audit, never blind rerun.
    marker = owner_home / "state" / ".i3-hmac-core-a-rotation-v1.attempted"
    require(
        not marker.exists() and not marker.is_symlink(),
        "I3 one-use private HMAC rotation has already been attempted",
    )
    storage.check_private(marker.parent, directory=True, sid=sid)
    PHASE = "one-use-attempt-marker"
    private_helper._new_private_file(
        marker,
        stage._json({"schema_version": 1, "old_core_a_key_id": old_a["key_id"]}),
        sid,
    )
    print("I3_ROTATE_ONE_USE_MARKER=PRIVATE")

    def provision(entries: list[dict]) -> None:
        expected_prior = original_grants if len(entries) == 3 else overlap_document
        require(
            read_private_json(grants_path, sid) == expected_prior,
            "I3 private grant file no longer matches the expected rotation phase",
        )
        document = {"schema_version": 1, "bridge_id": BRIDGE, "grants": entries}
        output = private_helper.run(
            [str(worker), "-I", "-B", "-c", stage._PROVISION, str(config)],
            input_data=stage._json(document),
            timeout=35,
        )
        require(
            output.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
            "I3 owner-only grant provision was unverified",
        )
        storage.check_private(grants_path, directory=False, sid=sid)
        require(
            read_private_json(grants_path, sid) == document,
            "I3 protected grants did not match expected state",
        )
        load_native_bridge_plan(bridge)

    from serverfs_mcp.agent_client import AgentBridgeRemoteError
    from serverfs_mcp.windows_v25.bridge_peer_job import make_bridge_peer_validator

    peer = make_bridge_peer_validator(bridge_id=BRIDGE, user_sid=sid)

    def client(binding):
        return binding.build_wiring(peer_validator=peer)[1]

    def verify_overlap() -> None:
        async def check():
            for binding in (*clients.values(), parsed_new):
                require(
                    await client(binding).call("bridge.ping", {"workdir": "Fixture"})
                    == {"status": "ok"},
                    "I3 overlap Core capability was rejected",
                )

        asyncio.run(asyncio.wait_for(check(), timeout=27))

    def verify_revoke() -> None:
        async def check():
            try:
                await client(clients[CORE_A]).call("bridge.ping", {"workdir": "Fixture"})
            except AgentBridgeRemoteError as exc:
                require(exc.code == "CORE_NOT_AUTHORIZED", "I3 retired key denial differed")
            else:
                raise I3RotateError("I3 retired private key still authorizes a ping")
            for binding in (parsed_new, clients[CORE_B]):
                require(
                    await client(binding).call("bridge.ping", {"workdir": "Fixture"})
                    == {"status": "ok"},
                    "I3 active rotated Core key was rejected",
                )

        asyncio.run(asyncio.wait_for(check(), timeout=27))

    started = False
    failure: BaseException | None = None
    fail_phase = ""
    stopped = False
    try:
        PHASE = "persist-overlapping-grants"
        provision([old_a, new_a_grant, old_b])
        print("I3_ROTATE_OVERLAP_GRANTS=PRIVATE")
        PHASE = "prove-live-overlap"
        started = True
        response = probe.cli("bridge", BRIDGE, "start")
        require(
            response.get("running") is True and response.get("readiness") == "owner_alive",
            "I3 overlap Bridge failed to become ready",
        )
        verify_overlap()
        print("I3_ROTATE_OVERLAP_OLD_NEW_AND_B=PASS")
        PHASE = "stop-before-binding-cutover"
        probe.cli("bridge", BRIDGE, "stop")
        _assert_disabled(probe, preflight)
        started = False

        PHASE = "replace-only-core-a-binding"
        binding_path = storage.home("core", CORE_A) / "bridge-binding.json"
        require(
            read_private_json(binding_path, sid) == original_bindings[CORE_A],
            "I3 Core A binding changed before cutover",
        )
        _private_replace(binding_path, stage._json(new_a_binding), sid)
        reloaded_workdirs, _ = load_native_config(Path(str(core[CORE_A].settings["config"])))
        replaced = load_native_core_bridge_binding(core[CORE_A], reloaded_workdirs)
        require(
            replaced.key_id == parsed_new.key_id and replaced.secret == parsed_new.secret,
            "I3 Core A private binding was not atomically published",
        )
        print("I3_ROTATE_CORE_A_BINDING=PRIVATE_NEW_KEY")

        PHASE = "revoke-old-private-core-a-grant"
        provision([new_a_grant, old_b])
        print("I3_ROTATE_OLD_A_KEY=REVOKED")
        PHASE = "prove-installed-revocation"
        started = True
        response = probe.cli("bridge", BRIDGE, "start")
        require(
            response.get("running") is True and response.get("readiness") == "owner_alive",
            "I3 rotated Bridge failed to become ready",
        )
        verify_revoke()
        print("I3_ROTATE_FRESH_NEW_A_AND_B=PASS")
    except BaseException as exc:
        failure = exc
        fail_phase = PHASE
    finally:
        PHASE = "isolated-final-stop"
        try:
            if started:
                probe.cli("bridge", BRIDGE, "stop")
            _assert_disabled(probe, preflight)
            stopped = True
        except Exception:
            stopped = False
    print("I3_ROTATE_FIXTURE_STOP=" + ("VERIFIED" if stopped else "NOT_VERIFIED"))
    require(stopped, "I3 rotation fixture stop unverified; do not retry")
    PHASE = "production-postflight"
    after = _current_identity(production)
    require(
        probe.production_snapshot(preflight) == baseline
        and after is not None
        and after.pid == production_before.pid
        and after.creation_time == production_before.creation_time,
        "production Core or Scheduler changed",
    )
    print("I3_ROTATE_PRODUCTION_TASKS_AND_CORE=UNCHANGED")
    if failure is not None:
        PHASE = fail_phase
        raise failure
    print("I3_INSTALLED_HMAC_ROTATION=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed phase/type only
        print(
            "I3_ROTATE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
