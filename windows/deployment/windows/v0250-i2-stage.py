"""Create-only I2 Bridge-v2 fixture installer (ordinary Windows user).

PRECONDITION: a clean I2 read-only preflight.  Builds private wheels/venvs and
registers only the new i2-smoke Bridge task as Disabled.  Never starts a service,
shares credentials with production, alters an incumbent task, or retries a
partial installation.  On failure leave private artifacts intact for diagnosis.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

INSTANCE = "i2-smoke"
REPO = Path(__file__).resolve().parents[2]
STAGE = "entry"

_PROVISION = """
import json
import sys
from pathlib import Path
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants import core_workdir_policy_digest
from serverfs_agent_bridge.core_grants_private import provision_private_core_grants
from serverfs_agent_bridge.store import TaskStore
config = BridgeConfig.load(Path(sys.argv[1]))
document = json.load(sys.stdin)
document["grants"][0]["workdirs"][0]["policy_sha256"] = (
    core_workdir_policy_digest(config.policies.get("Fixture"))
)
provision_private_core_grants(
    store=TaskStore(config.state_dir),
    bridge_id=config.bridge_id,
    policies=config.policies,
    document=document,
)
print("I2_GRANT_INSTALLED")
"""


class I2StageError(RuntimeError):
    """Fixed, redacted one-use installation refusal."""


def require(value: bool, message: str) -> None:
    if not value:
        raise I2StageError(message)


def _load_preflight():
    path = Path(__file__).with_name("v0250-i2-native-preflight.py")
    spec = importlib.util.spec_from_file_location("serverfs_i2_preflight", path)
    require(spec is not None and spec.loader is not None, "I2 preflight source is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.INSTANCE == INSTANCE, "I2 source identity mismatch")
    return module


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
    timeout: int = 180,
) -> bytes:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=input_data,
        stdin=subprocess.DEVNULL if input_data is None else None,
        capture_output=True,
        check=False,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise I2StageError("an isolated I2 step failed; preserve the stage marker")
    return result.stdout


def _protect_tree(root: Path, sid: str) -> None:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    storage.check_private(root, directory=True, sid=sid)
    for path in sorted(root.rglob("*"), key=lambda item: (len(item.parts), str(item).lower())):
        _no_reparse_components(path)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        require(
            not path.is_symlink()
            and not (info.st_file_attributes & 0x400)
            and (directory or stat.S_ISREG(info.st_mode)),
            "installed I2 package path is unsafe",
        )
        if not directory:
            require(info.st_nlink == 1, "installed I2 file is multiply linked")
        protect_new_file(path, sid)
        storage.check_private(path, directory=directory, sid=sid)


def _new_dir(path: Path, sid: str) -> None:
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    require(not path.exists() and not path.is_symlink(), "isolated directory already exists")
    path.mkdir()
    protect_new_file(path, sid)
    storage.check_private(path, directory=True, sid=sid)


def _new_private_file(path: Path, data: bytes, sid: str) -> None:
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    require(not path.exists() and not path.is_symlink(), "isolated input is occupied")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    protect_new_file(path, sid)
    storage.check_private(path, directory=False, sid=sid)


def _wheel(source: Path, output: Path, glob: str, checks: tuple[str, ...]) -> Path:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    run(["uv", "build", "--wheel", "--offline", "--out-dir", str(output)], cwd=source)
    wheels = tuple(output.glob("*.whl"))
    require(len(wheels) == 1 and wheels[0].match(glob), "exactly one approved I2 wheel expected")
    wheel = wheels[0]
    _no_reparse_components(wheel)
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "I2 package wheel is corrupt")
        package = "serverfs_mcp" if source == REPO else "serverfs_agent_bridge"
        for name in checks:
            require(
                archive.read(f"{package}/{name}") == (source / "src" / package / name).read_bytes(),
                "I2 wheel content differs from the current source",
            )
    return wheel


def _offline_install(venv: Path, wheels: list[Path]) -> tuple[Path, Path]:
    python = venv / "Scripts" / "python.exe"
    pythonw = venv / "Scripts" / "pythonw.exe"
    require(venv.is_dir() and not any(venv.iterdir()), "I2 venv must be new and empty")
    run(["uv", "venv", "--offline", "--python", "3.12.10", str(venv)])
    require(python.is_file() and pythonw.is_file(), "I2 venv executables are unavailable")
    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    command = ["uv", "pip", "install", "--offline", "--python", str(python), *map(str, wheels)]
    run([*command, "--dry-run"], env=env)
    run(command, env=env)
    return python, pythonw


def verify_installed_runtime(base: Path, owner_python: Path, worker_python: Path) -> None:
    """Prove the newly installed environments load the intended shipped code."""
    run(
        [
            str(owner_python),
            "-I",
            "-B",
            "-c",
            "import serverfs_mcp.windows_v25.runtime, serverfs_windows_native, mcp",
        ],
        timeout=35,
    )
    run(
        [
            str(worker_python),
            "-I",
            "-B",
            "-c",
            "import serverfs_agent_bridge.config, "
            "serverfs_agent_bridge.core_grants_private, "
            "serverfs_agent_bridge.protocol, serverfs_agent_bridge.main",
        ],
        timeout=35,
    )
    for installed, source in (
        (
            base
            / "envs"
            / "bridge"
            / "Lib"
            / "site-packages"
            / "serverfs_mcp"
            / "windows_v25"
            / "runtime.py",
            REPO / "src" / "serverfs_mcp" / "windows_v25" / "runtime.py",
        ),
        (
            base
            / "envs"
            / "agent-bridge"
            / "Lib"
            / "site-packages"
            / "serverfs_agent_bridge"
            / "core_grants_private.py",
            REPO / "agent_bridge" / "src" / "serverfs_agent_bridge" / "core_grants_private.py",
        ),
    ):
        require(
            installed.is_file() and installed.read_bytes() == source.read_bytes(),
            "I2 installed runtime differs from checked source",
        )


def _json(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> None:
    global STAGE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
    from serverfs_mcp.windows_v25.bridge_v2_owner import load_native_bridge_plan
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    STAGE = "read-only-native-preflight"
    gate = _load_preflight()
    gate.main()
    before = gate._scheduler_inventory()
    gate.verify_scheduler_rows(before)
    sid = current_user_sid()
    base = storage.root(INSTANCE)
    owner = storage.home("bridge", INSTANCE)
    resources = WindowsBridgeResources(sid, INSTANCE)
    native = REPO / "native" / "windows" / "target" / "wheels" / gate.NATIVE_WHEEL_NAME
    # Recheck the same immutable digest that was approved before first write.
    gate.verify_native_wheel(native)

    STAGE = "create-only-private-directories"
    storage.prepare("bridge", INSTANCE)
    _new_dir(base / "workdirs", sid)
    fixture = base / "workdirs" / "fixture"
    _new_dir(fixture, sid)
    build = base / "build"
    _new_dir(build, sid)
    product_dir = build / "core-owner"
    worker_dir = build / "bridge-worker"
    _new_dir(product_dir, sid)
    _new_dir(worker_dir, sid)
    print("I2_PRIVATE_DIRECTORIES=PASS")

    STAGE = "offline-wheels"
    product_wheel = _wheel(
        REPO,
        product_dir,
        "serverfs_mcp-*.whl",
        (
            "windows_v25/runtime.py",
            "windows_v25/bridge_v2_owner.py",
            "windows_v25/storage.py",
            "agent_core_grant_client.py",
        ),
    )
    worker_wheel = _wheel(
        REPO / "agent_bridge",
        worker_dir,
        "serverfs_agent_bridge-*.whl",
        ("config.py", "core_grants.py", "core_grants_private.py", "protocol.py"),
    )
    print("I2_WHEELS_MATCH_SOURCE=PASS")

    STAGE = "offline-private-venvs"
    owner_python, owner_pythonw = _offline_install(
        base / "envs" / "bridge", [product_wheel, native]
    )
    worker_python, _worker_pythonw = _offline_install(
        base / "envs" / "agent-bridge", [worker_wheel]
    )
    _protect_tree(base / "envs" / "bridge", sid)
    _protect_tree(base / "envs" / "agent-bridge", sid)
    _protect_tree(build, sid)
    print("I2_VENVS_PRIVATE=PASS")

    STAGE = "installed-runtime-verification"
    verify_installed_runtime(base, owner_python, worker_python)
    print("I2_INSTALLED_RUNTIME=VERIFIED")

    STAGE = "private-bridge-config"
    config_path = owner / "bridge.json"
    config = {
        "bridge_id": INSTANCE,
        "core_grants_enabled": True,
        "lease_key": "alias",
        "socket_path": resources.pipe_name,
        "state_dir": str(owner / "state"),
        "lock_dir": str(owner / "locks"),
        "allowed_peer_sid": sid,
        "workdirs": [
            {
                "alias": "Fixture",
                "host_path": str(fixture),
                "read_only": True,
                "agent_mode": "disabled",
            }
        ],
    }
    _new_private_file(config_path, _json(config), sid)
    secret = secrets.token_bytes(32)
    grant = {
        "schema_version": 1,
        "bridge_id": INSTANCE,
        "grants": [
            {
                "core_id": INSTANCE,
                "key_id": "fixture-health",
                "secret_hex": secret.hex(),
                "methods": ["bridge.ping"],
                "workdirs": [{"alias": "Fixture", "policy_sha256": "0" * 64}],
            }
        ],
    }
    # The independent Bridge package computes the authoritative full-policy
    # digest, validates the grant and creates its own protected state file.
    STAGE = "private-grant-issuance"
    result = run(
        [str(worker_python), "-I", "-B", "-c", _PROVISION, str(config_path)],
        input_data=_json(grant),
        timeout=35,
    )
    require(result.strip() == b"I2_GRANT_INSTALLED", "private grant issuance did not confirm")
    del grant, secret
    storage.check_private(owner / "state" / "core-grants.json", directory=False, sid=sid)
    print("I2_SIGNED_HEALTH_GRANT=PRIVATE")

    STAGE = "new-disabled-task-registration"
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": INSTANCE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(owner_pythonw),
            "settings": {"config": str(config_path), "bridge_python": str(worker_python)},
        }
    )
    require(owner_python.is_file(), "private Bridge owner Python is missing")
    load_native_bridge_plan(manifest)
    _assert_instance_isolation(manifest)
    gate.verify_scheduler_rows(gate._scheduler_inventory())
    storage.stage(manifest)
    installed = Scheduler().install(manifest)
    require(
        installed.get("registered") is True and not installed.get("enabled"),
        "I2 Bridge task was not registered Disabled",
    )
    current = Scheduler().status(manifest)
    require(
        current.get("state") == "Disabled" and not current.get("running"),
        "I2 Bridge task is not Disabled",
    )
    after = gate._scheduler_inventory()
    gate.verify_scheduler_rows([row for row in after if row.get("name") in gate.PRODUCTION])
    require(
        len(after) == len(before) + 1
        and any(
            row.get("name") == gate.TASK
            and row.get("state") == "Disabled"
            and row.get("enabled") is False
            for row in after
        ),
        "I2 post-install task identity is unverified",
    )
    print("I2_PRODUCTION_TASK_BASELINE=UNCHANGED")
    print("I2_BRIDGE_TASK_REGISTERED_DISABLED=PASS")
    print("I2_NATIVE_STAGE=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never expose stderr or private artifact values
        print(
            "I2_NATIVE_STAGE=FAILED phase=" + STAGE + " type=" + type(exc).__name__, file=sys.stderr
        )
        raise SystemExit(2) from None
