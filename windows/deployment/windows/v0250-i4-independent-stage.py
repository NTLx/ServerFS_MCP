"""Create-only I4 native installer: two independent *owned* Core/Bridge pairs.

Every Core's instance_id == its Bridge's bridge_id (bridge_mode=owned).
This is intentionally NOT a general management platform. It prepares only
two fixed, fresh instances i4-a/i4-b and registers exactly four Scheduler
tasks DISABLED; never starts services or touches production/I1/I2/I3 tasks.

Do not run if the matching read-only I4 preflight has not passed; do not retry
a failed/partial install. Retain partial private state for forensic review.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"
METHODS = ("bridge.ping", "runtime.list")


class I4StageError(RuntimeError):
    """Sanitized fail-closed I4 installation refusal."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise I4StageError(reason)


def helper(filename: str, name: str):
    file = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, file)
    require(spec is not None and spec.loader is not None, "I4 helper is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _bridge_policy(*, instance: str, sid: str, home: Path, fixture: Path) -> dict:
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

    resources = WindowsBridgeResources(sid, instance)
    return {
        "bridge_id": instance,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": resources.pipe_name,
        "state_dir": str(home / "state"),
        "lock_dir": str(home / "locks"),
        "allowed_peer_sid": sid,
        "enable_fake_runtime": False,
        "workdirs": [
            {
                "alias": "Fixture",
                "host_path": str(fixture),
                "read_only": True,
                "agent_mode": "review",
                "agent_runtimes": [],
                "repo_ops_enabled": False,
            }
        ],
    }


def _binding_and_grant(instance: str, digest: str) -> tuple[dict, dict]:
    """Create sealed per-instance owner capability without leaking it to output."""
    from serverfs_mcp.instance_identity import InstanceIdentity

    InstanceIdentity(instance, "owned", instance)
    key_id = "key-" + secrets.token_hex(8)
    secret = secrets.token_bytes(32).hex()
    scope = [{"alias": "Fixture", "policy_sha256": digest}]
    binding = {
        "schema_version": 1,
        "instance_id": instance,
        "bridge_mode": "owned",
        "bridge_id": instance,
        "key_id": key_id,
        "secret_hex": secret,
        "workdirs": scope,
    }
    grant = {
        "core_id": instance,
        "key_id": key_id,
        "secret_hex": secret,
        "methods": list(METHODS),
        "workdirs": scope,
    }
    return binding, grant


def main() -> None:
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation
    from serverfs_mcp.windows_v25.core_bridge_binding import (
        load_native_core_bridge_binding,
        parse_core_bridge_binding,
        verify_core_bridge_workdirs,
        verify_core_peer_bridge_owner,
    )
    from serverfs_mcp.windows_v25.isolation import assert_core_isolation
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    PHASE = "native-preflight"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I4 requires non-elevated native Python -I -B",
    )
    gate = helper("v0250-i4-independent-preflight.py", "i4_install_gate")
    gate.preflight()
    require(tuple(gate.BRIDGES) == ("i4-a", "i4-b"), "I4 owner IDs differ")
    require(gate.PAIRS == {"i4-a": "i4-a", "i4-b": "i4-b"}, "I4 owned binding differs")
    sid = current_user_sid()
    installer = helper("v0250-i2-stage.py", "i4_private_install_helpers")
    i3 = helper("v0250-i3-readonly-stage.py", "i4_installed_code_helpers")
    native_pin = helper("v0250-i2-native-preflight.py", "i4_native_wheel_gate")
    require(installer.REPO == i3.REPO == REPO, "I4 helper checkout identity differs")
    native = REPO / "native" / "windows" / "target" / "wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native)

    PHASE = "create-only-private-roots"
    for instance in gate.BRIDGES:
        storage.prepare("bridge", instance)
        storage.prepare("core", instance)
        base = storage.root(instance)
        installer._new_dir(base / "workdirs", sid)
        installer._new_dir(base / "workdirs" / "fixture", sid)
        installer._new_dir(base / "build", sid)
        installer._new_dir(base / "build" / "core-owner", sid)
        installer._new_dir(base / "build" / "bridge-worker", sid)
    print("I4_TWO_OWNED_PRIVATE_ROOTS=PASS")

    # Independent build wheels ensure either installation can later be
    # diagnosed or rebuilt without adopting the other installation's state.
    manifests = []
    PHASE = "private-wheels-and-venvs"
    for instance, port in gate.CORE_PORTS.items():
        base = storage.root(instance)
        product_wheel = installer._wheel(
            REPO,
            base / "build" / "core-owner",
            "serverfs_mcp-*.whl",
            (
                "windows_v25/runtime.py",
                "windows_v25/bridge_v2_owner.py",
                "windows_v25/core_bridge_binding.py",
                "agent_core_grant_client.py",
            ),
        )
        worker_wheel = installer._wheel(
            REPO / "agent_bridge",
            base / "build" / "bridge-worker",
            "serverfs_agent_bridge-*.whl",
            ("config.py", "core_grants.py", "core_grants_private.py", "protocol.py"),
        )
        installer._protect_tree(base / "build", sid)
        owner_python, owner_pythonw = i3._installed_core(
            installer,
            sid=sid,
            venv=base / "envs" / "bridge",
            wheels=[product_wheel, native],
        )
        worker_python, _worker_pythonw = installer._offline_install(
            base / "envs" / "agent-bridge", [worker_wheel]
        )
        installer._protect_tree(base / "envs" / "agent-bridge", sid)
        installer.verify_installed_runtime(base, owner_python, worker_python)
        core_python, core_pythonw = i3._installed_core(
            installer,
            sid=sid,
            venv=base / "envs" / "core",
            wheels=[product_wheel, native],
        )
        require(core_python.is_file(), "I4 Core interpreter is unavailable")

        PHASE = "private-owned-bridge-" + instance
        bridge_home = storage.home("bridge", instance)
        fixture = base / "workdirs" / "fixture"
        bridge_json = bridge_home / "bridge.json"
        bridge_doc = _bridge_policy(instance=instance, sid=sid, home=bridge_home, fixture=fixture)
        installer._new_private_file(bridge_json, _json(bridge_doc), sid)
        digest_raw = installer.run(
            [str(worker_python), "-I", "-B", "-c", i3._POLICY_SHA, str(bridge_json)],
            timeout=35,
        )
        digest = digest_raw.decode("ascii").strip()
        require(
            len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
            "I4 authoritative Bridge policy digest invalid",
        )

        PHASE = "private-owned-core-" + instance
        core_home = storage.home("core", instance)
        toml = core_home / "serverfs.toml"
        auth = core_home / "http.env"
        installer._new_private_file(toml, i3._core_policy(fixture), sid)
        workdirs, policy = load_native_config(toml)
        require(policy.bridge_required and len(workdirs) == 1, "I4 Core policy invalid")
        verify_core_bridge_workdirs(
            workdirs, bridge_doc, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )
        hostname = instance + ".local"
        http_key = secrets.token_urlsafe(48)
        raw_auth = (
            f"SERVERFS_HTTP_API_KEY={http_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n"
            "SERVERFS_OAUTH_ENABLED=false\n"
        )
        installer._new_private_file(auth, raw_auth.encode("utf-8"), sid)
        del http_key, raw_auth
        owner_manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "bridge",
                "instance_id": instance,
                "sid": sid,
                "pythonw": str(owner_pythonw),
                "settings": {
                    "config": str(bridge_json),
                    "bridge_python": str(worker_python),
                },
            }
        )
        core_manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "core",
                "instance_id": instance,
                "sid": sid,
                "pythonw": str(core_pythonw),
                "settings": {
                    "config": str(toml),
                    "auth_file": str(auth),
                    "port": port,
                    "hostname": hostname,
                },
            }
        )
        verify_core_peer_bridge_owner(owner_manifest, bridge_doc, owner_home=bridge_home)
        binding, grant = _binding_and_grant(instance, digest)
        grant_document = {"schema_version": 1, "bridge_id": instance, "grants": [grant]}
        result = installer.run(
            [str(worker_python), "-I", "-B", "-c", i3._PROVISION, str(bridge_json)],
            input_data=_json(grant_document),
            timeout=35,
        )
        require(
            result.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
            "I4 private owned-Bridge grant was not provisioned",
        )
        del grant, grant_document
        storage.check_private(bridge_home / "state" / "core-grants.json", directory=False, sid=sid)
        installer._new_private_file(core_home / "bridge-binding.json", _json(binding), sid)
        parse_core_bridge_binding(
            core_manifest,
            binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=owner_manifest,
            bridge_lock_dir=bridge_home / "locks",
        )
        del binding
        load_native_bridge_plan(owner_manifest)
        manifests.extend((owner_manifest, core_manifest))
    print("I4_SIX_PRIVATE_VENVS_AND_TWO_OWNED_GRANTS=VERIFIED")

    PHASE = "candidate-isolation-before-publication"
    require(len(manifests) == 4, "I4 owner inventory is incomplete")
    for manifest in manifests:
        if manifest.component == "bridge":
            _assert_instance_isolation(manifest)
        else:
            assert_core_isolation(
                manifest,
                (*storage.core_inventory(), *[m for m in manifests if m.component == "core"]),
            )

    # All private component credentials are already sealed before publishing
    # either pair. Register four exact new Disabled tasks; do not start them.
    PHASE = "private-manifest-publish"
    for manifest in manifests:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for manifest in manifests:
        if manifest.component == "core":
            workdirs, _policy = load_native_config(Path(str(manifest.settings["config"])))
            loaded = load_native_core_bridge_binding(manifest, workdirs)
            require(
                loaded.instance_id == loaded.bridge_id == manifest.instance_id
                and loaded.bridge_mode == "owned",
                "I4 Core owned Bridge binding differs",
            )
    print("I4_FOUR_PRIVATE_SERVICE_MANIFESTS=VERIFIED")

    PHASE = "disabled-task-registration"
    for manifest in manifests:
        result = Scheduler().install(manifest)
        require(
            result.get("registered") is True and result.get("enabled") is False,
            "I4 exact Scheduler registration was not Disabled",
        )
        state = Scheduler().status(manifest)
        require(
            state.get("state") == "Disabled" and state.get("running") is False,
            "I4 Scheduler task not Disabled",
        )
    gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)
    print("I4_PROTECTED_TASKS=UNCHANGED")
    print("I4_FOUR_TASKS_REGISTERED_DISABLED=PASS")
    print("I4_NATIVE_STAGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no secrets or raw exception text
        print(
            "I4_NATIVE_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
