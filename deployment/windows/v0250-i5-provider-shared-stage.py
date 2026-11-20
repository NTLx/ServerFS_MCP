"""One-use I5 provider-enabled shared Bridge + two Core native installer.

Three fresh private identities are created with no Provider task or model
execution: i5-shared Bridge, i5-core-a and i5-core-b HTTP Cores. All three
Task Scheduler entries are registered DISABLED. The writable workdir is
a private disposable Fixture, never any existing repository or production
workdir. Native Claude runtime is enabled only for an eventual scoped smoke.

If a step fails, preserve incomplete private state; never retry this script.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

BRIDGE = "i5-shared"
CORE_PORTS = {"i5-core-a": 18873, "i5-core-b": 18874}
REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"
METHODS = (
    "bridge.ping",
    "runtime.list",
    "task.submit",
    "task.get",
    "task.events",
    "task.result.read",
    "task.cancel",
    "task.approval.respond",
    "task.question.answer",
)

_DIGEST = """
from pathlib import Path
import sys
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
config = BridgeConfig.load(Path(sys.argv[1]))
policy = config.policies.get("Fixture")
if (
    not config.claude.enabled
    or policy.read_only
    or policy.mode.value != "workspace-write"
    or policy.runtimes != frozenset({"claude"})
    or policy.repo_ops_enabled
):
    raise ValueError("I5 Provider Fixture policy does not match")
print(core_workdir_policy_digest(policy))
"""


class I5StageError(RuntimeError):
    """I5 native install error with no path or credential disclosure."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5StageError(message)


def helper(filename: str, name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "I5 helper source missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(document: object) -> bytes:
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _bridge_document(*, home: Path, fixture: Path, sid: str, cli: str) -> dict:
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

    return {
        "bridge_id": BRIDGE,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": WindowsBridgeResources(sid, BRIDGE).pipe_name,
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


def _core_toml(fixture: Path) -> bytes:
    return (
        '[server]\nlog_level = "INFO"\n\n'
        "[agent]\nenabled = true\n\n"
        "[agent.claude]\nenabled = true\n\n"
        '[[workdirs]]\nalias = "Fixture"\n'
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        'read_only = false\nagent_mode = "workspace-write"\n'
        'agent_runtimes = ["claude"]\nrepo_ops_enabled = false\n'
    ).encode("utf-8")


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

    PHASE = "native-protected-preflight"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 native non-elevated Python -I -B required",
    )
    gate = helper("v0250-i5-provider-shared-preflight.py", "i5_private_host_gate")
    require(
        gate.BRIDGE == BRIDGE and gate.CORE_PORTS == CORE_PORTS,
        "I5 source identities disagree",
    )
    gate.preflight()
    sid = current_user_sid()

    installer = helper("v0250-i2-stage.py", "i5_private_installer_helpers")
    i3 = helper("v0250-i3-readonly-stage.py", "i5_installed_runtime_helpers")
    native_pin = helper("v0250-i2-native-preflight.py", "i5_pinned_native_wheel")
    require(installer.REPO == i3.REPO == REPO, "I5 reused helpers are from another checkout")
    native = REPO / "native" / "windows" / "target" / "wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native)
    cli = shutil.which("claude")
    require(
        isinstance(cli, str) and Path(cli).is_file() and os.access(cli, os.X_OK),
        "I5 native Claude CLI is not resolved",
    )
    # Pin this resolved CLI path in the private config; it is not a credential.
    cli = str(Path(cli).resolve(strict=True))

    PHASE = "create-only-private-instances"
    storage.prepare("bridge", BRIDGE)
    base = storage.root(BRIDGE)
    installer._new_dir(base / "workdirs", sid)
    fixture = base / "workdirs" / "fixture"
    installer._new_dir(fixture, sid)
    installer._new_dir(base / "build", sid)
    product_dir = base / "build" / "core-owner"
    worker_dir = base / "build" / "bridge-worker"
    installer._new_dir(product_dir, sid)
    installer._new_dir(worker_dir, sid)
    for core_id in CORE_PORTS:
        storage.prepare("core", core_id)
    print("I5_THREE_PRIVATE_INSTANCE_ROOTS=PASS")

    PHASE = "offline-pinned-installation"
    wheel = installer._wheel(
        REPO,
        product_dir,
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
        worker_dir,
        "serverfs_agent_bridge-*.whl",
        (
            "config.py",
            "core_grants_private.py",
            "protocol.py",
            "adapters/claude.py",
        ),
    )
    installer._protect_tree(base / "build", sid)
    owner_python, owner_pythonw = i3._installed_core(
        installer,
        sid=sid,
        venv=base / "envs" / "bridge",
        wheels=[wheel, native],
    )
    worker_python, _worker_pythonw = installer._offline_install(
        base / "envs" / "agent-bridge",
        [worker_wheel],
    )
    installer._protect_tree(base / "envs" / "agent-bridge", sid)
    installer.verify_installed_runtime(base, owner_python, worker_python)
    cores = {
        core_id: i3._installed_core(
            installer,
            sid=sid,
            venv=storage.root(core_id) / "envs" / "core",
            wheels=[wheel, native],
        )
        for core_id in CORE_PORTS
    }
    print("I5_FOUR_PRIVATE_VENVS=VERIFIED")

    PHASE = "private-provider-bridge-config"
    home = storage.home("bridge", BRIDGE)
    config_file = home / "bridge.json"
    config = _bridge_document(home=home, fixture=fixture, sid=sid, cli=cli)
    installer._new_private_file(config_file, _json(config), sid)
    digest_raw = installer.run(
        [str(worker_python), "-I", "-B", "-c", _DIGEST, str(config_file)],
        timeout=35,
    )
    digest = digest_raw.decode("ascii").strip()
    require(
        len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest),
        "I5 authoritative Provider policy digest unavailable",
    )
    print("I5_REAL_CLAUDE_RUNTIME_POLICY=VERIFIED")

    PHASE = "independent-core-policies"
    manifests = []
    for core_id, port in CORE_PORTS.items():
        core_home = storage.home("core", core_id)
        toml = core_home / "serverfs.toml"
        auth = core_home / "http.env"
        installer._new_private_file(toml, _core_toml(fixture), sid)
        workdirs, policy = load_native_config(toml)
        require(policy.bridge_required and len(workdirs) == 1, "I5 Core requires signed Bridge")
        verify_core_bridge_workdirs(
            workdirs,
            config,
            canonicalize=lambda raw: Path(raw).resolve(strict=True),
        )
        hostname = core_id + ".local"
        bearer = secrets.token_urlsafe(48)
        auth_contents = (
            f"SERVERFS_HTTP_API_KEY={bearer}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n"
            "SERVERFS_OAUTH_ENABLED=false\n"
        )
        installer._new_private_file(auth, auth_contents.encode("utf-8"), sid)
        del bearer, auth_contents
        _python, pythonw = cores[core_id]
        manifests.append(
            Manifest.parse(
                {
                    "schema": 2,
                    "instance_id": core_id,
                    "component": "core",
                    "sid": sid,
                    "pythonw": str(pythonw),
                    "settings": {
                        "config": str(toml),
                        "auth_file": str(auth),
                        "port": port,
                        "hostname": hostname,
                    },
                }
            )
        )
    print("I5_TWO_CORE_HTTP_CREDENTIALS=PRIVATE")

    PHASE = "private-two-core-hmac-grants"
    capabilities = issue_shared_core_capabilities(
        bridge_id=BRIDGE,
        core_ids=tuple(CORE_PORTS),
        workdir_policy_revisions={"Fixture": digest},
        methods=METHODS,
    )
    bridge_manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": BRIDGE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(owner_pythonw),
            "settings": {
                "config": str(config_file),
                "bridge_python": str(worker_python),
            },
        }
    )
    verify_core_peer_bridge_owner(bridge_manifest, config, owner_home=home)
    provision = {
        "schema_version": 1,
        "bridge_id": BRIDGE,
        "grants": [cap.bridge_grant() for cap in capabilities],
    }
    raw_result = installer.run(
        [str(worker_python), "-I", "-B", "-c", i3._PROVISION, str(config_file)],
        input_data=_json(provision),
        timeout=35,
    )
    require(
        raw_result.strip() == b"I3_PRIVATE_GRANTS_INSTALLED",
        "I5 native grants were not sealed by the Bridge worker",
    )
    del provision
    storage.check_private(home / "state" / "core-grants.json", directory=False, sid=sid)
    for manifest, capability in zip(manifests, capabilities, strict=True):
        binding = capability.core_binding()
        installer._new_private_file(
            storage.home("core", manifest.instance_id) / "bridge-binding.json",
            _json(binding),
            sid,
        )
        parse_core_bridge_binding(
            manifest,
            binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=bridge_manifest,
            bridge_lock_dir=home / "locks",
        )
        del binding
    del capabilities
    load_native_bridge_plan(bridge_manifest)
    print("I5_TWO_PRIVATE_SIGNED_CORE_GRANTS=VERIFIED")

    PHASE = "strict-source-and-owner-isolation"
    _assert_instance_isolation(bridge_manifest)
    for manifest in manifests:
        assert_core_isolation(manifest, (*storage.core_inventory(), *manifests))
    all_manifests = [bridge_manifest, *manifests]
    for manifest in all_manifests:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for manifest in manifests:
        workdirs, _policy = load_native_config(Path(str(manifest.settings["config"])))
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "shared"
            and binding.bridge_id == BRIDGE
            and binding.instance_id == manifest.instance_id,
            "I5 private Core signed shared identity differs",
        )
    print("I5_THREE_PRIVATE_SERVICE_MANIFESTS=VERIFIED")

    PHASE = "register-disabled-only"
    for manifest in all_manifests:
        state = Scheduler().install(manifest)
        require(
            state.get("registered") is True and state.get("enabled") is False,
            "I5 service registration not Disabled",
        )
        check = Scheduler().status(manifest)
        require(
            check.get("state") == "Disabled" and check.get("running") is False,
            "I5 disabled Task Scheduler state not verified",
        )
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=True)
    print("I5_PROTECTED_TASKS=UNCHANGED")
    print("I5_THREE_TASKS_REGISTERED_DISABLED=PASS")
    print("I5_NATIVE_STAGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - failure only gives fixed phase/type
        print(
            "I5_NATIVE_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
