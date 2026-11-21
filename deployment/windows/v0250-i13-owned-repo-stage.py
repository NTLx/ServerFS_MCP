"""I13 Windows native: one-use install of TWO Repo-only owned Core/Bridge pairs.

Creates two NEW SID-private disposable Git roots (no remote, no real user
repository), each with one signed owned Bridge, Core, per-Worker private
grants and one SHA256-pinned fixed status_probe Git action. Both expose Repo
Ops but NO Agent runtime, no model, no Connector. Exactly four Windows
Task Scheduler owners registered DISABLED; no process start. One-use only:
if partial, preserve evidence, do NOT retry or delete.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IDS = ("i13-a", "i13-b")
PORTS = {"i13-a": 18883, "i13-b": 18884}
GUARD = b"I13_FIXED_REPO_INTEGRITY_GUARD\n"
ACTION = "status_probe"
METHODS = (
    "bridge.ping",
    "runtime.list",
    "repo.inspect",
    "repo.action",
    "repo.commit",
    "repo.push",
)
PHASE = "entry"

_DIGEST = """
from pathlib import Path
import sys
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
config = BridgeConfig.load(Path(sys.argv[1]))
policy = config.policies.get("Fixture")
if (
    policy is None or policy.read_only or policy.mode.value != "disabled"
    or policy.runtimes or not policy.repo_ops_enabled
    or len(policy.repo_actions) != 1
    or policy.repo_actions[0].name != "status_probe"
    or policy.repo_actions[0].mutates
):
    raise ValueError("I13 Repo-only private policy differs")
print(core_workdir_policy_digest(policy))
"""


class I13StageRefusal(RuntimeError):
    """Never disclose private paths, Bearers, signed grants or Git stdout."""


def require(ok: bool, label: str) -> None:
    if not ok:
        raise I13StageRefusal(label)


def helper(name: str, label: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "I13 trusted helper absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def bridge_policy(instance: str, bridge_home: Path, fixture: Path, sid: str, git: str) -> dict:
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources

    return {
        "bridge_id": instance,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": WindowsBridgeResources(sid, instance).pipe_name,
        "state_dir": str(bridge_home / "state"),
        "lock_dir": str(bridge_home / "locks"),
        "allowed_peer_sid": sid,
        "enable_fake_runtime": False,
        "workdirs": [
            {
                "alias": "Fixture",
                "host_path": str(fixture),
                "read_only": False,
                "agent_mode": "disabled",
                "agent_runtimes": [],
                "repo_ops_enabled": True,
                "repo_actions": {
                    ACTION: {
                        "argv": [git, "status", "--short"],
                        "mutates": False,
                        "argument": "none",
                        "integrity": {"guard.txt": hashlib.sha256(GUARD).hexdigest()},
                    }
                },
            }
        ],
    }


def core_policy(fixture: Path, git: str) -> bytes:
    return (
        '[server]\nlog_level = "INFO"\n\n'
        '[[workdirs]]\nalias = "Fixture"\n'
        + "path = "
        + json.dumps(str(fixture), ensure_ascii=False)
        + "\n"
        + 'read_only = false\nagent_mode = "disabled"\n'
        + "agent_runtimes = []\nrepo_ops_enabled = true\n\n"
        + f"[workdirs.repo_actions.{ACTION}]\n"
        + "argv = "
        + json.dumps([git, "status", "--short"])
        + "\n"
        + 'mutates = false\nargument = "none"\n\n'
        + f"[workdirs.repo_actions.{ACTION}.integrity]\n"
        + '"guard.txt" = "'
        + hashlib.sha256(GUARD).hexdigest()
        + '"\n'
    ).encode("utf-8")


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
        "I13 ordinary Windows SID -I -B required",
    )


def fresh_git(fixture: Path, git: str, sid: str, installer) -> None:
    """Create a self-contained local-only repo; never touch user Git identity."""
    from serverfs_mcp.windows_v25 import storage

    guard = fixture / "guard.txt"
    installer._new_private_file(guard, GUARD, sid)
    # Explicitly isolate Git configuration from global/system or remote helpers.
    env = {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "USERPROFILE") if key in os.environ
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(fixture / "never-created-global.ini")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "file"
    actions = (
        ["init", "--initial-branch=main"],
        ["config", "--local", "user.name", "ServerFS I13 Test"],
        ["config", "--local", "user.email", "serverfs-i13@example.invalid"],
        ["config", "--local", "commit.gpgSign", "false"],
        ["config", "--local", "core.hooksPath", os.devnull],
        ["add", "--", "guard.txt"],
        ["commit", "--no-gpg-sign", "-m", "I13 disposable fixture root"],
    )
    for argv in actions:
        installer.run([git, *argv], cwd=fixture, env=env, timeout=45)
    del env
    installer._protect_tree(fixture, sid)
    storage.check_private(guard, directory=False, sid=sid)
    storage.check_private(fixture / ".git/config", directory=False, sid=sid)
    storage.check_private(fixture / ".git/HEAD", directory=False, sid=sid)
    require(
        guard.read_bytes() == GUARD and (fixture / ".git").is_dir(),
        "I13 local-only Git fixture lost exact guard bytes",
    )


def resume_audit() -> None:
    """READ ONLY: verify the precise failed first-run checkpoint, nothing else."""
    global PHASE
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    PHASE = "resume-native-identity-and-protected-baseline"
    native()
    helper("v0250-i11-two-owned-provider-stage.py", "i13_resume_protected_existing").postflight()
    PHASE = "resume-private-task-namespace-and-ports"
    pre = helper("v0250-i13-owned-repo-preflight.py", "i13_resume_private_names")
    require(pre.IDS == IDS and pre.PORTS == PORTS, "I13 resume identity mismatch")
    pre.verify_tasks(pre.task_rows(), installed=False)
    require(
        not any(p.port in PORTS.values() for p in _windows_ipv4_tcp_listeners()),
        "I13 resume selected port is occupied",
    )
    sid = current_user_sid()
    git = shutil.which("git")
    require(isinstance(git, str) and Path(git).is_file(), "I13 resume Git unavailable")
    git = str(Path(git).resolve(strict=True))
    PHASE = "resume-two-private-git-fixture-roots"
    for instance in IDS:
        root = storage.root(instance)
        storage.check_private(root, directory=True, sid=sid)
        fixture = root / "workdirs/fixture"
        guard = fixture / "guard.txt"
        storage.check_private(fixture, directory=True, sid=sid)
        storage.check_private(guard, directory=False, sid=sid)
        storage.check_private(fixture / ".git/config", directory=False, sid=sid)
        storage.check_private(fixture / ".git/HEAD", directory=False, sid=sid)
        require(guard.read_bytes() == GUARD, "I13 partial Git guard differs")
        for component in ("bridge", "core"):
            home = storage.home(component, instance)
            storage.check_private(home, directory=True, sid=sid)
            require(
                not (home / "service.json").exists() and not (home / "service.json").is_symlink(),
                "I13 resume encountered a published native owner",
            )
        require(
            not (storage.home("core", instance) / "http.env").exists()
            and not (storage.home("core", instance) / "bridge-binding.json").exists()
            and not (storage.home("bridge", instance) / "state/core-grants.json").exists(),
            "I13 partial checkpoint already contains credentials/grants",
        )
    PHASE = "resume-installed-a-private-policy"
    a = storage.root(IDS[0])
    a_fixture = a / "workdirs/fixture"
    b = storage.root(IDS[1])
    bridge_config = storage.home("bridge", IDS[0]) / "bridge.json"
    core_config = storage.home("core", IDS[0]) / "serverfs.toml"
    for item in (bridge_config, core_config):
        storage.check_private(item, directory=False, sid=sid)
    expected_bridge = bridge_policy(IDS[0], storage.home("bridge", IDS[0]), a_fixture, sid, git)
    require(
        bridge_config.read_bytes() == _json(expected_bridge)
        and core_config.read_bytes() == core_policy(a_fixture, git),
        "I13 exact previous private policy checkpoint differs",
    )
    PHASE = "resume-installed-a-native-repo-config"
    workdirs, settings = load_native_config(core_config)
    require(
        settings.bridge_required
        and settings.repo_ops_enabled
        and len(workdirs) == 1
        and workdirs[0].policy.repo_ops_enabled
        and len(settings.repo_actions.get("Fixture", ())) == 1,
        "I13 native Repo action model mismatch",
    )
    PHASE = "resume-installed-a-private-interpreters"
    for component in ("bridge", "core", "agent-bridge"):
        python = a / "envs" / component / "Scripts/python.exe"
        storage.check_private(python, directory=False, sid=sid)
    PHASE = "resume-b-not-yet-installed"
    for component in ("bridge", "core", "agent-bridge"):
        require(
            not (b / "envs" / component / "Scripts/python.exe").exists(),
            "I13 B was already installed at failed checkpoint",
        )
    PHASE = "resume-one-use-marker-untouched"
    for item in (
        storage.home("bridge", IDS[1]) / "bridge.json",
        storage.home("core", IDS[1]) / "serverfs.toml",
        a / "repo-resume.attempted",
    ):
        require(not item.exists() and not item.is_symlink(), "I13 resume already attempted")
    PHASE = "resume-readonly-accepted"
    print("I13_FIRST_STAGE_PARTIAL_EXACT_AND_PRIVATE=VERIFIED")
    print("I13_FOUR_TASKS_UNREGISTERED_AND_PORTS_UNBOUND=VERIFIED")
    print("I13_PROTECTED_I1_I11_PRODUCTION=UNCHANGED")
    print("I13_ONE_USE_RESUME_READONLY_AUDIT=PASS")


def stage(*, resume: bool = False) -> None:
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

    native()
    pre = helper("v0250-i13-owned-repo-preflight.py", "i13_exact_fresh_namespace")
    require(pre.IDS == IDS and pre.PORTS == PORTS, "I13 stage/preflight identities differ")
    PHASE = "protected-preflight-and-unused-two-repo-pairs"
    if resume:
        resume_audit()
    else:
        pre.preflight()
    sid = current_user_sid()
    git = shutil.which("git")
    require(isinstance(git, str) and Path(git).is_file(), "I13 Git executable unavailable")
    git = str(Path(git).resolve(strict=True))
    installer = helper("v0250-i2-stage.py", "i13_sid_private_installer")
    i3 = helper("v0250-i3-readonly-stage.py", "i13_wheel_installer")
    i4 = helper("v0250-i4-independent-stage.py", "i13_owned_capability_factory")
    pin = helper("v0250-i2-native-preflight.py", "i13_native_binary_pin")
    require(installer.REPO == i3.REPO == i4.REPO == REPO, "I13 source provenance mismatch")
    native_wheel = REPO / "native/windows/target/wheels" / pin.NATIVE_WHEEL_NAME
    pin.verify_native_wheel(native_wheel)

    if resume:
        PHASE = "one-use-private-resume-marker"
        installer._new_private_file(
            storage.root(IDS[0]) / "repo-resume.attempted",
            b"I13_ONE_USE_EXACT_CORE_POLICY_CHECKPOINT\n",
            sid,
        )
        print("I13_EXACT_PARTIAL_RESUME_MARKER=SEALED")
    else:
        PHASE = "create-only-two-new-private-Git-repo-roots"
        for instance in IDS:
            storage.prepare("bridge", instance)
            storage.prepare("core", instance)
            root = storage.root(instance)
            for directory in (
                root / "workdirs",
                root / "workdirs/fixture",
                root / "build",
                root / "build/core-owner",
                root / "build/bridge-worker",
            ):
                installer._new_dir(directory, sid)
            fresh_git(root / "workdirs/fixture", git, sid, installer)
        print("I13_TWO_NEW_SID_PRIVATE_GIT_REPOS_NO_REMOTE=PASS")

    PHASE = "two-offline-native-Wheel-installs"
    manifests: list[Manifest] = []
    for instance, port in PORTS.items():
        root = storage.root(instance)
        fixture = root / "workdirs/fixture"
        if resume and instance == IDS[0]:
            bridge_python = root / "envs/bridge/Scripts/python.exe"
            bridge_pythonw = root / "envs/bridge/Scripts/pythonw.exe"
            worker_python = root / "envs/agent-bridge/Scripts/python.exe"
            core_python = root / "envs/core/Scripts/python.exe"
            core_pythonw = root / "envs/core/Scripts/pythonw.exe"
            for installed in (
                bridge_python,
                bridge_pythonw,
                worker_python,
                core_python,
                core_pythonw,
            ):
                storage.check_private(installed, directory=False, sid=sid)
            installer.verify_installed_runtime(root, bridge_python, worker_python)
        else:
            product_wheel = installer._wheel(
                REPO,
                root / "build/core-owner",
                "serverfs_mcp-*.whl",
                (
                    "windows_v25/runtime.py",
                    "windows_v25/bridge_v2_owner.py",
                    "windows_v25/core_bridge_binding.py",
                    "agent_core_grant_client.py",
                    "repo_tools.py",
                ),
            )
            worker_wheel = installer._wheel(
                REPO / "agent_bridge",
                root / "build/bridge-worker",
                "serverfs_agent_bridge-*.whl",
                ("config.py", "core_grants_private.py", "protocol.py", "repo_ops_windows.py"),
            )
            installer._protect_tree(root / "build", sid)
            bridge_python, bridge_pythonw = i3._installed_core(
                installer,
                sid=sid,
                venv=root / "envs/bridge",
                wheels=[product_wheel, native_wheel],
            )
            worker_python, _worker_pythonw = installer._offline_install(
                root / "envs/agent-bridge", [worker_wheel]
            )
            installer._protect_tree(root / "envs/agent-bridge", sid)
            installer.verify_installed_runtime(root, bridge_python, worker_python)
            core_python, core_pythonw = i3._installed_core(
                installer,
                sid=sid,
                venv=root / "envs/core",
                wheels=[product_wheel, native_wheel],
            )
            require(core_python.is_file() and core_pythonw.is_file(), "I13 Core Python missing")
        print("I13_PRIVATE_OFFLINE_RUNTIME_" + instance.upper().replace("-", "_") + "=PASS")

        PHASE = "private-owned-repo-Bridge-policy-" + instance
        bridge_home = storage.home("bridge", instance)
        core_home = storage.home("core", instance)
        config = bridge_home / "bridge.json"
        bridge_doc = bridge_policy(instance, bridge_home, fixture, sid, git)
        if resume and instance == IDS[0]:
            storage.check_private(config, directory=False, sid=sid)
            require(config.read_bytes() == _json(bridge_doc), "I13 resumable Bridge policy drift")
        else:
            installer._new_private_file(config, _json(bridge_doc), sid)
        digest = (
            installer.run(
                [str(worker_python), "-I", "-B", "-c", _DIGEST, str(config)],
                timeout=35,
            )
            .decode("ascii")
            .strip()
        )
        require(
            len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
            "I13 private policy digest malformed",
        )

        PHASE = "private-owned-repo-Core-policy-" + instance
        toml = core_home / "serverfs.toml"
        auth = core_home / "http.env"
        if resume and instance == IDS[0]:
            storage.check_private(toml, directory=False, sid=sid)
            require(
                toml.read_bytes() == core_policy(fixture, git), "I13 resumable Core policy drift"
            )
        else:
            installer._new_private_file(toml, core_policy(fixture, git), sid)
        workdirs, settings = load_native_config(toml)
        require(
            settings.bridge_required
            and settings.repo_ops_enabled
            and len(workdirs) == 1
            and workdirs[0].read_only is False
            and workdirs[0].agent_mode == "disabled"
            and workdirs[0].policy.repo_ops_enabled
            and len(settings.repo_actions.get("Fixture", ())) == 1,
            "I13 Core Repo-only policy mismatch",
        )
        verify_core_bridge_workdirs(
            workdirs, bridge_doc, canonicalize=lambda text: Path(text).resolve(strict=True)
        )
        token = secrets.token_urlsafe(48)
        installer._new_private_file(
            auth,
            (
                f"SERVERFS_HTTP_API_KEY={token}\n"
                f"SERVERFS_HTTP_ALLOWED_HOSTS={instance}.local,127.0.0.1:{port}\n"
                "SERVERFS_OAUTH_ENABLED=false\n"
            ).encode(),
            sid,
        )
        del token
        bridge_manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "bridge",
                "instance_id": instance,
                "sid": sid,
                "pythonw": str(bridge_pythonw),
                "settings": {"config": str(config), "bridge_python": str(worker_python)},
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
                    "hostname": instance + ".local",
                },
            }
        )
        verify_core_peer_bridge_owner(bridge_manifest, bridge_doc, owner_home=bridge_home)

        PHASE = "two-separate-signed-repo-method-grants-" + instance
        binding, grant = i4._binding_and_grant(instance, digest)
        grant["methods"] = list(METHODS)
        result = installer.run(
            [str(worker_python), "-I", "-B", "-c", i3._PROVISION, str(config)],
            input_data=_json(
                {
                    "schema_version": 1,
                    "bridge_id": instance,
                    "grants": [grant],
                }
            ),
            timeout=35,
        )
        require(result.strip() == b"I3_PRIVATE_GRANTS_INSTALLED", "I13 signed Repo grant failed")
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
        del binding, grant
        manifests.extend((bridge_manifest, core_manifest))
    require(len(manifests) == 4, "I13 staged four owners mismatch")
    print("I13_TWO_ISOLATED_OWNED_REPO_CORE_GRANTS=PASS")

    PHASE = "isolated-manifest-publication-only"
    for manifest in manifests:
        if manifest.component == "bridge":
            _assert_instance_isolation(manifest)
        else:
            assert_core_isolation(
                manifest,
                (*storage.core_inventory(), *(m for m in manifests if m.component == "core")),
            )
    for manifest in manifests:
        storage.stage(manifest)
        storage.load(manifest.component, manifest.instance_id)
    for instance in IDS:
        manifest = storage.load("core", instance)
        config_path = Path(str(manifest.settings["config"]))
        workdirs, _settings = load_native_config(config_path)
        binding = load_native_core_bridge_binding(manifest, workdirs)
        require(
            binding.bridge_mode == "owned" and binding.instance_id == binding.bridge_id == instance,
            "I13 installed Core binding differs from its owned Bridge",
        )
    print("I13_FOUR_SIGNED_PRIVATE_OWNER_MANIFESTS=PASS")

    PHASE = "register-only-four-Disabled-native-Tasks"
    for manifest in manifests:
        status = Scheduler().install(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("running") is False,
            "I13 native Task Scheduler registration not Disabled",
        )
    postflight()
    print("I13_TWO_INDEPENDENT_REPO_ONLY_OWNED_PAIRS_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.core_bridge_binding import load_native_core_bridge_binding
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    old = helper("v0250-i11-two-owned-provider-stage.py", "i13_post_protected_i11")
    old.postflight()
    gate = helper("v0250-i13-owned-repo-preflight.py", "i13_exact_four_private_tasks")
    gate.verify_tasks(gate.task_rows(), installed=True)
    fixtures = []
    for instance, port in PORTS.items():
        root = storage.root(instance)
        fixture = root / "workdirs/fixture"
        storage.check_private(fixture, directory=True, sid=storage._sid())
        guard = fixture / "guard.txt"
        storage.check_private(guard, directory=False, sid=storage._sid())
        require(guard.read_bytes() == GUARD, "I13 immutable repo guard bytes differ")
        require((fixture / ".git").is_dir(), "I13 isolated local Git missing")
        fixtures.append(fixture.resolve(strict=True))
        bridge = storage.load("bridge", instance)
        core = storage.load("core", instance)
        require(
            bridge.instance_id == core.instance_id == instance and core.settings["port"] == port,
            "I13 owned Pair manifests differ",
        )
        workdirs, settings = load_native_config(Path(str(core.settings["config"])))
        require(
            settings.bridge_required
            and settings.repo_ops_enabled
            and len(workdirs) == 1
            and workdirs[0].root.resolve(strict=True) == fixtures[-1]
            and not workdirs[0].read_only
            and workdirs[0].agent_mode == "disabled"
            and not workdirs[0].policy.agent_runtimes
            and workdirs[0].policy.repo_ops_enabled
            and len(settings.repo_actions.get("Fixture", ())) == 1,
            "I13 Repo-only Core config or actions widened",
        )
        binding = load_native_core_bridge_binding(core, workdirs)
        require(
            binding.bridge_mode == "owned" and binding.bridge_id == binding.instance_id == instance,
            "I13 installed signed owner binding invalid",
        )
        load_native_bridge_plan(bridge)
        for manifest in (bridge, core):
            status = Scheduler().status(manifest)
            require(
                status.get("registered") is True
                and status.get("enabled") is False
                and status.get("running") is False
                and status.get("state") == "Disabled"
                and status.get("readiness") == "offline"
                and _current_identity(manifest) is None,
                "I13 owner still running or not Disabled",
            )
        for component in ("core", "bridge", "agent-bridge"):
            storage.check_private(
                root / "envs" / component / "Scripts/python.exe",
                directory=False,
                sid=storage._sid(),
            )
        for component, item in (
            ("core", "http.env"),
            ("core", "bridge-binding.json"),
            ("bridge", "state/core-grants.json"),
        ):
            storage.check_private(
                storage.home(component, instance) / item,
                directory=False,
                sid=storage._sid(),
            )
    require(fixtures[0] != fixtures[1], "I13 two Git workdirs overlap")
    print("I13_TWO_PRIVATE_REPO_GUARDS_AND_OWNED_WORKERS=PASS")
    print("I13_FOUR_DISABLED_REPO_TASK_OWNERS_AND_GRANTS=PASS")
    print("I13_PRODUCTION_AND_I4_I11=UNCHANGED")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", action="store_true")
    action.add_argument("--resume-audit", action="store_true")
    action.add_argument("--resume-after-i13-a-core-policy", action="store_true")
    action.add_argument("--postflight", action="store_true")
    flags = parser.parse_args()
    if flags.stage:
        stage()
    elif flags.resume_audit:
        resume_audit()
    elif flags.resume_after_i13_a_core_policy:
        stage(resume=True)
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - class/phase only
        print(
            "I13_REPO_OWNED_INSTALL=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
