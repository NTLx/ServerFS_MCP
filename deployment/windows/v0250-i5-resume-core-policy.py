"""I5 exact known-partial-state repair and one-use disabled task registration.

Never rerun the create-only I5 stage. This file is the ONLY recovery entry
from its first failure at independent-core-policies/NativeConfigError.
The standalone read-only exact-state diagnostic must pass IN THIS PROCESS
before any private artifact can be written.

Restores Core A TOML atomically with an owner-private original copy; completes
Core B, two private HTTP keys, HMAC Core grants/bindings and three Disabled
Task Scheduler entries. No model requests, Agent tasks or service starts.
Do not rerun if a recovery step fails. Preserve the partial state.
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import sys
from pathlib import Path

PHASE = "entry"


class I5ResumeError(RuntimeError):
    """Sanitized one-use, exact-state recovery refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5ResumeError(reason)


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
    from serverfs_mcp.windows_v25.shared_core_grants import issue_shared_core_capabilities
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows -I -B required",
    )
    import importlib.util

    def helper(filename: str, label: str):
        source = Path(__file__).with_name(filename)
        spec = importlib.util.spec_from_file_location(label, source)
        require(spec is not None and spec.loader is not None, "known recovery helper absent")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    stage = helper("v0250-i5-provider-shared-stage.py", "i5_known_original_stage")
    diagnostic = helper("v0250-i5-postfail-readonly.py", "i5_verified_partial")
    i2 = helper("v0250-i2-stage.py", "i5_private_artifact_writer")
    i3 = helper("v0250-i3-readonly-stage.py", "i5_worker_provisioner")
    gate = helper("v0250-i5-provider-shared-preflight.py", "i5_protected_scheduler_gate")

    PHASE = "exact-partial-state-preflight"
    require(
        stage.BRIDGE == gate.BRIDGE
        and stage.CORE_PORTS == gate.CORE_PORTS
        and i2.REPO == stage.REPO == i3.REPO,
        "I5 private installer identities differ",
    )
    diagnostic.inspect()
    sid = current_user_sid()
    print("I5_RESUME_EXACT_PARTIAL_STATE=VERIFIED")

    bridge_root = storage.root(stage.BRIDGE)
    bridge_home = storage.home("bridge", stage.BRIDGE)
    fixture = bridge_root / "workdirs" / "fixture"
    bridge_config = bridge_home / "bridge.json"
    storage.check_private(bridge_config, directory=False, sid=sid)
    require(
        bridge_config.stat().st_size < 256 * 1024,
        "I5 Bridge config size invalid",
    )
    config = json.loads(bridge_config.read_text(encoding="utf-8"))
    worker_python = bridge_root / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    bridge_pythonw = bridge_root / "envs" / "bridge" / "Scripts" / "pythonw.exe"
    storage.check_private(worker_python, directory=False, sid=sid)
    storage.check_private(bridge_pythonw, directory=False, sid=sid)

    PHASE = "atomic-original-core-policy-repair"
    first_id = next(iter(stage.CORE_PORTS))
    first_home = storage.home("core", first_id)
    policy_file = first_home / "serverfs.toml"
    backup = first_home / "serverfs.original-invalid.toml"
    replacement = first_home / "serverfs.new-verified.toml"
    old = diagnostic.historical_core_toml(fixture)
    require(
        policy_file.read_bytes() == old
        and not backup.exists()
        and not replacement.exists()
        and not backup.is_symlink()
        and not replacement.is_symlink(),
        "I5 original Core policy or repair slots differ",
    )
    correct = stage._core_toml(fixture)
    require(correct != old, "I5 Core policy generator has not been repaired")
    # Validate the replacement using the same real TOML parser before
    # replacing the existing protected Core A file.
    i2._new_private_file(backup, old, sid)
    i2._new_private_file(replacement, correct, sid)
    workdirs_a, policy_a = load_native_config(replacement)
    require(
        policy_a.bridge_required
        and policy_a.agent is not None
        and policy_a.agent.claude.enabled
        and len(workdirs_a) == 1
        and workdirs_a[0].agent_runtimes == frozenset({"claude"}),
        "repaired I5 Core A policy is not enabled for Claude",
    )
    verify_core_bridge_workdirs(
        workdirs_a, config, canonicalize=lambda raw: Path(raw).resolve(strict=True)
    )
    os.replace(replacement, policy_file)
    storage.check_private(policy_file, directory=False, sid=sid)
    require(policy_file.read_bytes() == correct, "I5 repaired Core A TOML differs")
    print("I5_RESUME_CORE_A_POLICY=REPAIRED_WITH_PRIVATE_ORIGINAL")

    PHASE = "second-core-policy-and-private-http-credentials"
    manifests = []
    for core_id, port in stage.CORE_PORTS.items():
        home = storage.home("core", core_id)
        toml = home / "serverfs.toml"
        if core_id != first_id:
            i2._new_private_file(toml, stage._core_toml(fixture), sid)
        storage.check_private(toml, directory=False, sid=sid)
        workdirs, settings = load_native_config(toml)
        require(
            settings.bridge_required
            and settings.agent is not None
            and settings.agent.claude.enabled
            and len(workdirs) == 1,
            "I5 Core enabled policy is invalid",
        )
        verify_core_bridge_workdirs(
            workdirs, config, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )
        hostname = core_id + ".local"
        api_key = secrets.token_urlsafe(48)
        auth_content = (
            f"SERVERFS_HTTP_API_KEY={api_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n"
            "SERVERFS_OAUTH_ENABLED=false\n"
        )
        auth = home / "http.env"
        i2._new_private_file(auth, auth_content.encode("utf-8"), sid)
        del api_key, auth_content
        core_pythonw = storage.root(core_id) / "envs" / "core" / "Scripts" / "pythonw.exe"
        storage.check_private(core_pythonw, directory=False, sid=sid)
        manifests.append(
            Manifest.parse(
                {
                    "schema": 2,
                    "instance_id": core_id,
                    "component": "core",
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
        )
    print("I5_RESUME_TWO_CORE_CLAUDE_POLICIES_AND_HTTP_KEYS=PRIVATE")

    PHASE = "private-bridge-hmac-two-core-grants"
    digest_raw = i2.run(
        [str(worker_python), "-I", "-B", "-c", stage._DIGEST, str(bridge_config)],
        timeout=35,
    )
    digest = digest_raw.decode("ascii").strip()
    require(
        len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest),
        "I5 original Bridge policy revision unavailable",
    )
    capabilities = issue_shared_core_capabilities(
        bridge_id=stage.BRIDGE,
        core_ids=tuple(stage.CORE_PORTS),
        workdir_policy_revisions={"Fixture": digest},
        methods=stage.METHODS,
    )
    bridge_manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": stage.BRIDGE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(bridge_pythonw),
            "settings": {
                "config": str(bridge_config),
                "bridge_python": str(worker_python),
            },
        }
    )
    verify_core_peer_bridge_owner(bridge_manifest, config, owner_home=bridge_home)
    grants = {
        "schema_version": 1,
        "bridge_id": stage.BRIDGE,
        "grants": [cap.bridge_grant() for cap in capabilities],
    }
    result = i2.run(
        [str(worker_python), "-I", "-B", "-c", i3._PROVISION, str(bridge_config)],
        input_data=stage._json(grants),
        timeout=35,
    )
    require(
        result.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
        "I5 Bridge did not confirm its own sealed grants",
    )
    del grants
    storage.check_private(bridge_home / "state" / "core-grants.json", directory=False, sid=sid)
    for manifest, capability in zip(manifests, capabilities, strict=True):
        binding = capability.core_binding()
        i2._new_private_file(
            storage.home("core", manifest.instance_id) / "bridge-binding.json",
            stage._json(binding),
            sid,
        )
        parse_core_bridge_binding(
            manifest,
            binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=bridge_manifest,
            bridge_lock_dir=bridge_home / "locks",
        )
        del binding
    del capabilities
    load_native_bridge_plan(bridge_manifest)
    print("I5_RESUME_TWO_SIGNED_SHARED_CORE_GRANTS=PRIVATE")

    PHASE = "private-owner-and-core-manifests"
    _assert_instance_isolation(bridge_manifest)
    for manifest in manifests:
        assert_core_isolation(manifest, (*storage.core_inventory(), *manifests))
    all_manifests = [bridge_manifest, *manifests]
    for manifest in all_manifests:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for manifest in manifests:
        workdirs, _settings = load_native_config(Path(str(manifest.settings["config"])))
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "shared"
            and binding.bridge_id == stage.BRIDGE
            and binding.instance_id == manifest.instance_id,
            "I5 verified Core grant binding differs",
        )
    print("I5_RESUME_THREE_PRIVATE_MANIFESTS=VERIFIED")

    PHASE = "register-three-disabled-only"
    for manifest in all_manifests:
        installed = Scheduler().install(manifest)
        require(
            installed.get("registered") is True and installed.get("enabled") is False,
            "I5 Scheduler candidate not Disabled",
        )
        state = Scheduler().status(manifest)
        require(
            state.get("state") == "Disabled" and state.get("running") is False,
            "I5 Scheduler installed task not Disabled",
        )
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    print("I5_RESUME_PRODUCTION_AND_I1_I4=UNCHANGED")
    print("I5_RESUME_THREE_TASKS_REGISTERED_DISABLED=PASS")
    print("I5_NATIVE_STAGE_RECOVERED=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only phase/type, no private path or keys
        print(
            "I5_NATIVE_RESUME=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
