"""I11 one-use native Windows install of TWO independent Provider-enabled owned pairs.

Two fresh per-user, per-instance roots i11-a and i11-b. Each independently
installs native Core, Bridge Owner and Claude Worker offline, with a new
SID-private disposable writable Fixture, distinct Bridge pipe, HMAC secret,
key ID, HTTP Bearer, lock and SQLite state. The selected model runtime is
enabled in policy but NO Provider task or model call is issued during stage.

Exactly 4 native Task Scheduler owners are installed DISABLED only. No
Cloudflare/OpenAI Connector, DNS, production, I4, I5 or Git modification.
If partial stage fails: preserve evidence, never retry this create-only step.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IDS = ("i11-a", "i11-b")
PORTS = {"i11-a": 18881, "i11-b": 18882}
PHASE = "entry"


class I11StageRefusal(RuntimeError):
    """No raw credentials, task XML or private file contents are logged."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I11StageRefusal(reason)


def helper(name: str, label: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "fixed Windows stage helper absent")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json(data: object) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _bridge_document(instance: str, home: Path, fixture: Path, sid: str, cli: str) -> dict:
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

    return {
        "bridge_id": instance,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": WindowsBridgeResources(sid, instance).pipe_name,
        "state_dir": str(home / "state"),
        "lock_dir": str(home / "locks"),
        "allowed_peer_sid": sid,
        "enable_fake_runtime": False,
        "claude": {"enabled": True, "claude_bin": cli, "use_proxy": False},
        "workdirs": [
            {
                "alias": "Fixture",
                "host_path": str(fixture),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["claude"],
                "repo_ops_enabled": False,
            }
        ],
    }


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
        "nonadmin native Windows -I -B required",
    )


def stage() -> None:
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

    PHASE = "preflight-native-protected-empty-I11-namespace"
    native()
    pre = helper("v0250-i11-two-owned-provider-preflight.py", "i11_provider_new_pair_gate")
    require(pre.IDS == IDS and pre.PORTS == PORTS, "I11 stage identity mismatch")
    pre.preflight()
    sid = current_user_sid()

    i4 = helper("v0250-i4-independent-stage.py", "i11_owned_binding_factory")
    i5 = helper("v0250-i5-provider-shared-stage.py", "i11_provider_policy_factory")
    installer = helper("v0250-i2-stage.py", "i11_private_installer")
    i3 = helper("v0250-i3-readonly-stage.py", "i11_offline_core_installer")
    native_pin = helper("v0250-i2-native-preflight.py", "i11_native_extension_pin")
    require(
        installer.REPO == i3.REPO == REPO
        and i4.REPO == i5.REPO == REPO
        and len(i5.METHODS) >= 7
        and "task.cancel" in i5.METHODS,
        "I11 exact source provenance/provider capability drift",
    )
    native_wheel = REPO / "native/windows/target/wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native_wheel)
    cli = shutil.which("claude")
    require(
        isinstance(cli, str) and Path(cli).is_file() and os.access(cli, os.X_OK),
        "I11 native Claude CLI unavailable",
    )
    cli = str(Path(cli).resolve(strict=True))

    PHASE = "create-only-two-owned-private-fixtures"
    for instance in IDS:
        storage.prepare("bridge", instance)
        storage.prepare("core", instance)
        base = storage.root(instance)
        for directory in (
            base / "workdirs",
            base / "workdirs/fixture",
            base / "build",
            base / "build/core-owner",
            base / "build/bridge-worker",
        ):
            installer._new_dir(directory, sid)
    print("I11_TWO_DISJOINT_SID_PRIVATE_OWNED_ROOTS=PASS")

    staged: list[Manifest] = []
    PHASE = "two-disjoint-offline-Wheel-installs"
    for instance, port in PORTS.items():
        base = storage.root(instance)
        fixture = base / "workdirs/fixture"
        product_wheel = installer._wheel(
            REPO,
            base / "build/core-owner",
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
            base / "build/bridge-worker",
            "serverfs_agent_bridge-*.whl",
            ("config.py", "core_grants_private.py", "protocol.py", "adapters/claude.py"),
        )
        installer._protect_tree(base / "build", sid)
        owner_python, owner_pythonw = i3._installed_core(
            installer, sid=sid, venv=base / "envs/bridge", wheels=[product_wheel, native_wheel]
        )
        worker_python, _worker_pythonw = installer._offline_install(
            base / "envs/agent-bridge", [worker_wheel]
        )
        installer._protect_tree(base / "envs/agent-bridge", sid)
        installer.verify_installed_runtime(base, owner_python, worker_python)
        core_python, core_pythonw = i3._installed_core(
            installer, sid=sid, venv=base / "envs/core", wheels=[product_wheel, native_wheel]
        )
        require(
            core_python.is_file() and core_pythonw.is_file(),
            "I11 private Core interpreter missing",
        )
        print("I11_PRIVATE_TWO_VENVS_" + instance.upper().replace("-", "_") + "=PASS")

        PHASE = "owned-provider-Bridge-policy-" + instance
        bridge_home = storage.home("bridge", instance)
        config_path = bridge_home / "bridge.json"
        bridge_doc = _bridge_document(instance, bridge_home, fixture, sid, cli)
        installer._new_private_file(config_path, _json(bridge_doc), sid)
        raw_digest = installer.run(
            [str(worker_python), "-I", "-B", "-c", i5._DIGEST, str(config_path)],
            timeout=35,
        )
        digest = raw_digest.decode("ascii").strip()
        require(
            len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest),
            "I11 authoritative bridge policy digest invalid",
        )

        PHASE = "owned-provider-Core-policy-" + instance
        core_home = storage.home("core", instance)
        config_core = core_home / "serverfs.toml"
        auth = core_home / "http.env"
        installer._new_private_file(config_core, i5._core_toml(fixture), sid)
        workdirs, policy = load_native_config(config_core)
        require(
            policy.bridge_required
            and len(workdirs) == 1
            and workdirs[0].read_only is False
            and workdirs[0].policy.repo_ops_enabled is False
            and workdirs[0].policy.agent_runtimes == frozenset({"claude"}),
            "I11 Core provider-only disposable policy mismatch",
        )
        verify_core_bridge_workdirs(
            workdirs, bridge_doc, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )
        import secrets

        bearer = secrets.token_urlsafe(48)
        installer._new_private_file(
            auth,
            (
                f"SERVERFS_HTTP_API_KEY={bearer}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS={instance}.local,127.0.0.1:{port}\n"
                "SERVERFS_OAUTH_ENABLED=false\n"
            ).encode(),
            sid,
        )
        del bearer
        bridge_manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "bridge",
                "instance_id": instance,
                "sid": sid,
                "pythonw": str(owner_pythonw),
                "settings": {
                    "config": str(config_path),
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
                    "config": str(config_core),
                    "auth_file": str(auth),
                    "port": port,
                    "hostname": instance + ".local",
                },
            }
        )
        verify_core_peer_bridge_owner(bridge_manifest, bridge_doc, owner_home=bridge_home)

        PHASE = "sealed-owned-grant-and-binding-" + instance
        binding, grant = i4._binding_and_grant(instance, digest)
        require(
            binding["bridge_mode"] == "owned"
            and binding["instance_id"] == binding["bridge_id"] == instance
            and grant["core_id"] == instance,
            "I11 isolated owned binding identity differs",
        )
        grant["methods"] = list(i5.METHODS)
        grants = {
            "schema_version": 1,
            "bridge_id": instance,
            "grants": [grant],
        }
        result = installer.run(
            [str(worker_python), "-I", "-B", "-c", i3._PROVISION, str(config_path)],
            input_data=_json(grants),
            timeout=35,
        )
        require(
            result.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
            "I11 private signed Core grant not installed",
        )
        storage.check_private(bridge_home / "state/core-grants.json", directory=False, sid=sid)
        installer._new_private_file(core_home / "bridge-binding.json", _json(binding), sid)
        parse_core_bridge_binding(
            core_manifest,
            binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=bridge_manifest,
            bridge_lock_dir=bridge_home / "locks",
        )
        load_native_bridge_plan(bridge_manifest)
        del grant, grants, binding
        staged.extend((bridge_manifest, core_manifest))
    print("I11_TWO_INDEPENDENT_PROVIDER_WORKERS_AND_SIGNED_GRANTS=PASS")

    PHASE = "isolation-audit-before-four-manifest-publication"
    require(len(staged) == 4, "I11 exact pair manifest count differs")
    for manifest in staged:
        if manifest.component == "bridge":
            _assert_instance_isolation(manifest)
        else:
            assert_core_isolation(
                manifest,
                (*storage.core_inventory(), *(m for m in staged if m.component == "core")),
            )
    for manifest in staged:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for instance in IDS:
        core_manifest = storage.load("core", instance)
        workdirs, policy = load_native_config(Path(str(core_manifest.settings["config"])))
        require(policy.bridge_required, "I11 staged Core policy unexpectedly file-only")
        capability = load_native_core_bridge_binding(core_manifest, workdirs)
        require(
            capability.bridge_mode == "owned"
            and capability.bridge_id == capability.instance_id == instance,
            "I11 staged signed owned binding mismatch",
        )
    print("I11_EXACT_FOUR_PRIVATE_CORE_BRIDGE_MANIFESTS=PASS")

    PHASE = "register-only-four-disabled-Scheduler-owners"
    for manifest in staged:
        installed = Scheduler().install(manifest)
        require(
            installed.get("registered") is True
            and installed.get("enabled") is False
            and installed.get("running") is False,
            "I11 native owner was not registered Disabled",
        )
    print("I11_FOUR_INDEPENDENT_DISABLED_NATIVE_SCHEDULER_TASKS=PASS")
    postflight()
    print("I11_TWO_OWNED_PROVIDER_PAIRS_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    pre = helper("v0250-i11-two-owned-provider-preflight.py", "i11_postexact_gate")
    pre.native()
    pre.verify_tasks(pre.candidate_tasks(), installed=True)
    pre.helper(
        "v0250-i7-connectors-disabled-register.py",
        "i11_post_existing_i7",
    ).postflight()
    ids = []
    for instance, port in PORTS.items():
        base = storage.root(instance)
        fixture = base / "workdirs/fixture"
        storage.check_private(fixture, directory=True, sid=storage._sid())
        ids.append(fixture.resolve(strict=True))
        core = storage.load("core", instance)
        bridge = storage.load("bridge", instance)
        require(
            core.component == "core"
            and bridge.component == "bridge"
            and core.instance_id == bridge.instance_id == instance
            and core.settings["port"] == port
            and core.settings["hostname"] == instance + ".local",
            "I11 staged independent owned pair identity changed",
        )
        workdirs, settings = load_native_config(Path(str(core.settings["config"])))
        require(
            len(workdirs) == 1
            and settings.bridge_required
            and workdirs[0].read_only is False
            and workdirs[0].policy.repo_ops_enabled is False
            and workdirs[0].policy.agent_runtimes == frozenset({"claude"})
            and workdirs[0].root.resolve(strict=True) == ids[-1],
            "I11 independent disposable Provider policy changed",
        )
        binding = load_native_core_bridge_binding(core, workdirs)
        require(
            binding.bridge_mode == "owned" and binding.bridge_id == binding.instance_id == instance,
            "I11 installed owned-Bridge binding invalid",
        )
        load_native_bridge_plan(bridge)
        for manifest in (bridge, core):
            status = Scheduler().status(manifest)
            require(
                status.get("registered") is True
                and status.get("enabled") is False
                and status.get("state") == "Disabled"
                and status.get("running") is False
                and status.get("readiness") == "offline"
                and _current_identity(manifest) is None,
                "I11 staged owner is not Disabled/offline",
            )
        storage.check_private(
            storage.home("core", instance) / "http.env", directory=False, sid=storage._sid()
        )
        storage.check_private(
            storage.home("core", instance) / "bridge-binding.json",
            directory=False,
            sid=storage._sid(),
        )
        storage.check_private(
            storage.home("bridge", instance) / "state/core-grants.json",
            directory=False,
            sid=storage._sid(),
        )
        for component in ("core", "bridge", "agent-bridge"):
            path = base / "envs" / component / "Scripts/python.exe"
            storage.check_private(path, directory=False, sid=storage._sid())
    require(ids[0] != ids[1], "I11 two writable Fixture directories unexpectedly shared")
    print("I11_TWO_DISJOINT_NATIVE_OWNED_CLAUDE_WORKERS=PASS")
    print("I11_FOUR_OWNERS_DISABLED_WITH_PRIVATE_GRANTS=PASS")
    print("I11_PRODUCTION_AND_I4_I7_UNCHANGED=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    if args.stage:
        stage()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - stable class+phase only
        print(
            "I11_OWNED_PROVIDER_INSTALL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
