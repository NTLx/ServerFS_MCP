"""One-use I3 read-only shared-Bridge + two native HTTP Core installer.

The i3-shared Bridge and both i3 Core identities are new and isolated.
All three Scheduler tasks are registered DISABLED.  The Worker has zero
enabled runtime providers and zero Repo Ops; no Agent task may execute.

This is stage 1 of I3: signed shared IPC and two real HTTP Cores can later
be exercised with a separate scoped start/stop test. Native I3 Task ownership
and writable Agent/Repo operations are NOT established by this installer.

Do not rerun on failure. Preserve partial private installation for evidence.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

BRIDGE = "i3-shared"
CORE_PORTS = {"i3-core-a": 18869, "i3-core-b": 18870}
REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"
_METHODS = ("bridge.ping", "runtime.list")

_POLICY_SHA = """
from pathlib import Path
import sys
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
config = BridgeConfig.load(Path(sys.argv[1]))
policy = config.policies.get("Fixture")
if policy is None or not policy.read_only or policy.mode.value != "review" or policy.runtimes:
    raise ValueError("I3 isolated read-only policy differs")
print(core_workdir_policy_digest(policy))
"""

_PROVISION = """
import json
from pathlib import Path
import sys
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.store import TaskStore
config = BridgeConfig.load(Path(sys.argv[1]))
document = json.load(sys.stdin)
provision_private_core_grants(
    store=TaskStore(config.state_dir),
    bridge_id=config.bridge_id,
    policies=config.policies,
    document=document,
)
print("I3_PRIVATE_GRANTS_INSTALLED")
"""


class I3StageError(RuntimeError):
    """Redacted create-only shared-Bridge stage refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I3StageError(reason)


def load_helper(filename: str, label: str):
    source = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, source)
    require(spec is not None and spec.loader is not None, "I3 staged helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _core_policy(fixture: Path) -> bytes:
    return (
        '[server]\nlog_level = "INFO"\n\n'
        "[agent]\nenabled = true\n\n"
        '[[workdirs]]\nalias = "Fixture"\n'
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        'read_only = true\nagent_mode = "review"\n'
        "agent_runtimes = []\nrepo_ops_enabled = false\n"
    ).encode("utf-8")


def _bridge_policy(*, sid: str, home: Path, fixture: Path) -> dict:
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

    resources = WindowsBridgeResources(sid, BRIDGE)
    return {
        "bridge_id": BRIDGE,
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


def _installed_core(
    helper,
    *,
    sid: str,
    venv: Path,
    wheels: list[Path],
) -> tuple[Path, Path]:
    from serverfs_mcp.windows_v25 import storage

    python, pythonw = helper._offline_install(venv, wheels)
    helper._protect_tree(venv, sid)
    helper.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "import serverfs_mcp.windows_v25.runtime, "
            "serverfs_mcp.windows_v25.core_bridge_binding, "
            "serverfs_windows_native, mcp",
        ],
        timeout=35,
    )
    for relative in (
        Path("windows_v25") / "runtime.py",
        Path("windows_v25") / "core_bridge_binding.py",
        Path("agent_core_grant_client.py"),
    ):
        installed = venv / "Lib" / "site-packages" / "serverfs_mcp" / relative
        source = REPO / "src" / "serverfs_mcp" / relative
        storage.check_private(installed, directory=False, sid=sid)
        require(installed.read_bytes() == source.read_bytes(), "I3 Core installed code differs")
    return python, pythonw


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
    from serverfs_mcp.windows_v25.shared_core_grants import issue_shared_core_capabilities
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    PHASE = "native-host-preflight"
    require(
        os.name == "nt"
        and sys.platform == "win32"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I3 stage requires non-elevated native Windows Python -I -B",
    )
    gate = load_helper("v0250-i3-shared-preflight.py", "i3_original_preflight")
    gate.main()
    sid = current_user_sid()
    helper = load_helper("v0250-i2-stage.py", "i3_offline_private_helpers")
    require(helper.REPO == REPO, "I3 private helper checkout identity differs")

    PHASE = "new-protected-directories"
    base = storage.root(BRIDGE)
    storage.prepare("bridge", BRIDGE)
    helper._new_dir(base / "workdirs", sid)
    fixture = base / "workdirs" / "fixture"
    helper._new_dir(fixture, sid)
    helper._new_dir(base / "build", sid)
    core_wheels = base / "build" / "core-owner"
    worker_wheels = base / "build" / "bridge-worker"
    helper._new_dir(core_wheels, sid)
    helper._new_dir(worker_wheels, sid)
    for core_id in CORE_PORTS:
        storage.prepare("core", core_id)
    print("I3_PRIVATE_INSTANCE_ROOTS=PASS")

    PHASE = "offline-private-packages"
    product_wheel = helper._wheel(
        REPO,
        core_wheels,
        "serverfs_mcp-*.whl",
        (
            "windows_v25/runtime.py",
            "windows_v25/bridge_v2_owner.py",
            "windows_v25/core_bridge_binding.py",
            "windows_v25/shared_core_grants.py",
            "agent_core_grant_client.py",
        ),
    )
    worker_wheel = helper._wheel(
        REPO / "agent_bridge",
        worker_wheels,
        "serverfs_agent_bridge-*.whl",
        ("config.py", "core_grants.py", "core_grants_private.py", "protocol.py"),
    )
    helper._protect_tree(base / "build", sid)
    print("I3_OFFLINE_WHEELS=VERIFIED")

    PHASE = "isolated-venv-installs"
    # Reuse the pinned native digest gate from the accepted I2 installer.
    pinned = load_helper("v0250-i2-native-preflight.py", "i3_native_wheel_pin")
    native = REPO / "native" / "windows" / "target" / "wheels" / pinned.NATIVE_WHEEL_NAME
    pinned.verify_native_wheel(native)
    bridge_python, bridge_pythonw = _installed_core(
        helper,
        sid=sid,
        venv=base / "envs" / "bridge",
        wheels=[product_wheel, native],
    )
    worker_python, _ = helper._offline_install(base / "envs" / "agent-bridge", [worker_wheel])
    helper._protect_tree(base / "envs" / "agent-bridge", sid)
    helper.verify_installed_runtime(base, bridge_python, worker_python)
    cores: dict[str, tuple[Path, Path]] = {}
    for core_id in CORE_PORTS:
        cores[core_id] = _installed_core(
            helper,
            sid=sid,
            venv=storage.root(core_id) / "envs" / "core",
            wheels=[product_wheel, native],
        )
    print("I3_FOUR_PRIVATE_VENVS=VERIFIED")

    PHASE = "read-only-bridge-policy"
    home = storage.home("bridge", BRIDGE)
    config_path = home / "bridge.json"
    config = _bridge_policy(sid=sid, home=home, fixture=fixture)
    helper._new_private_file(config_path, _json(config), sid)
    raw = helper.run(
        [str(worker_python), "-I", "-B", "-c", _POLICY_SHA, str(config_path)],
        timeout=35,
    )
    digest = raw.decode("ascii").strip()
    require(
        len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
        "I3 worker policy digest unavailable",
    )
    print("I3_SHARED_READONLY_POLICY=VERIFIED")

    PHASE = "private-core-policy-and-auth"
    core_manifests = []
    for core_id, port in CORE_PORTS.items():
        core_home = storage.home("core", core_id)
        toml_path = core_home / "serverfs.toml"
        auth_path = core_home / "http.env"
        helper._new_private_file(toml_path, _core_policy(fixture), sid)
        workdirs, settings = load_native_config(toml_path)
        require(settings.bridge_required and len(workdirs) == 1, "I3 Core policy invalid")
        verify_core_bridge_workdirs(
            workdirs, config, canonicalize=lambda raw: Path(raw).resolve(strict=True)
        )
        hostname = f"{core_id}.local"
        secret = secrets.token_urlsafe(48)
        payload = (
            f"SERVERFS_HTTP_API_KEY={secret}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={hostname},127.0.0.1:{port}\n"
            "SERVERFS_OAUTH_ENABLED=false\n"
        )
        helper._new_private_file(auth_path, payload.encode("utf-8"), sid)
        del secret, payload
        _python, pythonw = cores[core_id]
        core_manifests.append(
            Manifest.parse(
                {
                    "schema": 2,
                    "instance_id": core_id,
                    "component": "core",
                    "sid": sid,
                    "pythonw": str(pythonw),
                    "settings": {
                        "config": str(toml_path),
                        "auth_file": str(auth_path),
                        "hostname": hostname,
                        "port": port,
                    },
                }
            )
        )
    print("I3_TWO_PRIVATE_CORE_POLICIES=VERIFIED")

    PHASE = "private-two-core-grants"
    capabilities = issue_shared_core_capabilities(
        bridge_id=BRIDGE,
        core_ids=tuple(CORE_PORTS),
        workdir_policy_revisions={"Fixture": digest},
        methods=_METHODS,
    )
    owner_manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": BRIDGE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(bridge_pythonw),
            "settings": {"config": str(config_path), "bridge_python": str(worker_python)},
        }
    )
    verify_core_peer_bridge_owner(owner_manifest, config, owner_home=home)
    private_grants = {
        "schema_version": 1,
        "bridge_id": BRIDGE,
        "grants": [cap.bridge_grant() for cap in capabilities],
    }
    answer = helper.run(
        [str(worker_python), "-I", "-B", "-c", _PROVISION, str(config_path)],
        input_data=_json(private_grants),
        timeout=35,
    )
    require(answer.strip() == b"I3_PRIVATE_GRANTS_INSTALLED", "I3 private grants not confirmed")
    del private_grants
    storage.check_private(home / "state" / "core-grants.json", directory=False, sid=sid)
    for manifest, capability in zip(core_manifests, capabilities, strict=True):
        private_binding = capability.core_binding()
        helper._new_private_file(
            storage.home("core", manifest.instance_id) / "bridge-binding.json",
            _json(private_binding),
            sid,
        )
        parse_core_bridge_binding(
            manifest,
            private_binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=owner_manifest,
            bridge_lock_dir=home / "locks",
        )
        del private_binding
    del capabilities
    load_native_bridge_plan(owner_manifest)
    print("I3_TWO_DISTINCT_HMAC_GRANTS=PRIVATE")

    PHASE = "register-disabled-tasks"
    _assert_instance_isolation(owner_manifest)
    for manifest in core_manifests:
        from serverfs_mcp.windows_v25.isolation import assert_core_isolation

        assert_core_isolation(manifest, (*storage.core_inventory(), *core_manifests))
    installed_manifests = [owner_manifest, *core_manifests]
    for manifest in installed_manifests:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for manifest in core_manifests:
        workdirs, _settings = load_native_config(Path(str(manifest.settings["config"])))
        load_native_core_bridge_binding(manifest, workdirs)
    print("I3_THREE_SERVICE_MANIFESTS=PRIVATE")
    for manifest in installed_manifests:
        outcome = Scheduler().install(manifest)
        require(
            outcome.get("registered") is True and outcome.get("enabled") is False,
            "I3 exact Task Scheduler Disabled registration failed",
        )
        verified = Scheduler().status(manifest)
        require(
            verified.get("state") == "Disabled" and not verified.get("running"),
            "I3 Task Scheduler status not Disabled",
        )
    # Verify the protected incumbent tasks once more; also prove all three
    # independent test identities are present and Disabled, no extras.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", gate.SCRIPT],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I3 postflight Scheduler inventory unavailable")
    inventory = json.loads(result.stdout.decode("utf-8-sig"))
    require(isinstance(inventory, list), "I3 postflight inventory invalid")
    production = [
        row for row in inventory if isinstance(row, dict) and row.get("name") in gate.PRODUCTION
    ]
    gate.verify_task_inventory(production)
    require(
        len(inventory) == len(gate.PRODUCTION) + len(gate.TARGETS)
        and all(
            sum(row.get("name") == task for row in inventory) == 1
            and next(row for row in inventory if row.get("name") == task).get("state") == "Disabled"
            and next(row for row in inventory if row.get("name") == task).get("enabled") is False
            for task in gate.TARGETS
        ),
        "I3 exact Disabled registration postflight unavailable",
    )
    print("I3_PRODUCTION_TASKS=UNCHANGED")
    print("I3_THREE_TASKS=REGISTERED_DISABLED")
    print("I3_NATIVE_STAGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - no secrets, paths or full errors
        print(
            "I3_NATIVE_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
