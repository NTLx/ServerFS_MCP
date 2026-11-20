"""I14: temporary two independent Cloudflare Connectors for I13 owned Repo pairs.

NONPRODUCTION only. Exact i13-a/b owners and independent existing I7 Tunnel A/B.
--audit is read-only; --stage is one-use: copies credentials into distinct
current-SID-private homes and registers exactly two DISABLED Scheduler tasks.
No Cloudflare control-plane, DNS, public request or service activation here.
Do not rerun stage following partial failure. Never print credential material.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SLOT = "i14-cloudflare-owner-1"
ROUTES = (
    ("i13-a", "i14-edge-a", "i7-edge-a", 18883, "workpc-v25a.ntlx.top"),
    ("i13-b", "i14-edge-b", "i7-edge-b", 18884, "workpc-v25b.ntlx.top"),
)
PHASE = "entry"


class I14StageRefusal(RuntimeError):
    """Nonsecret refusal, even on Windows native task/ACL failure."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I14StageRefusal(message)


def helper(filename: str, label: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "trusted fixture helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def baseline():
    from serverfs_mcp.windows_v25 import storage

    native()
    i13 = helper("v0250-i13-owned-repo-stage.py", "i14_existing_owned_repo")
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i14_original_nonprod_tunnels")
    i13pre = helper("v0250-i13-owned-repo-preflight.py", "i14_exact_owned_namespace")
    i13.postflight()
    i7.postflight()
    require(
        i13.IDS == ("i13-a", "i13-b")
        and i13.PORTS == {"i13-a": 18883, "i13-b": 18884}
        and i7.IDS == ("i7-edge-a", "i7-edge-b"),
        "installed native fixture identities drifted",
    )
    require(
        {
            f"ServerFS v25 {core} Cloudflare Connector {connector}"
            for core, connector, _old, _port, _host in ROUTES
        }
        == i13pre.OPTIONAL_CONNECTOR_NAMES,
        "optional Connector task names mismatch",
    )
    base = storage.root("i7-public")
    owner = base / "envs/cloudflare"
    binary = base / "services/cloudflare/bin/cloudflared.exe"
    sid = storage._sid()
    for candidate in (owner / "Scripts/python.exe", owner / "Scripts/pythonw.exe", binary):
        storage.check_private(candidate, directory=False, sid=sid)
        require(candidate.is_file() and not candidate.is_symlink(), "pinned owner unavailable")
    pin = helper("v0250-i7-nonprod-core-stage.py", "i14_cloudflared_digest")
    require(pin.sha(binary) == pin.CLOUDFLARED_SHA256, "I7 cloudflared binary changed")
    return i13, i7, i13pre, owner, binary


def fresh() -> tuple:
    from serverfs_mcp.windows_v25 import storage

    i13, i7, pre, _owner, _binary = baseline()
    require(
        {row["name"] for row in pre.task_rows()} == pre.TASK_NAMES,
        "temporary Connector namespace occupied",
    )
    require(set(i13.IDS) == {row[0] for row in ROUTES}, "Core targets drifted")
    for core, connector, old, port, _host in ROUTES:
        require(i13.PORTS[core] == port, "Core listener port drifted")
        home = storage.home("cloudflare", core, connector)
        require(not home.exists() and not home.is_symlink(), "temporary private home exists")
        base = storage.root(core)
        for candidate in (
            base / "envs/cloudflare/Scripts/pythonw.exe",
            base / "services/cloudflare/bin",
            base / "build" / SLOT,
        ):
            require(
                not candidate.exists() and not candidate.is_symlink(),
                "private Connector Owner installation slot occupied",
            )
        original = storage.home("cloudflare", "i7-public", old) / i7.TOKEN_NAME
        _digest, _account, tunnel = i7.private_token_record(original, sid=storage._sid())
        require(tunnel == i7.EXPECTED_TUNNELS[old], "original nonproduction Tunnel differs")
    return i13, i7, pre, _owner, _binary


def stage() -> None:
    global PHASE
    from serverfs_mcp.instance_identity import InstanceIdentity
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    _i13, i7, _pre, _owner, binary = fresh()
    installer = helper("v0250-i2-stage.py", "i14_sid_private_file_creator")
    installed = helper("v0250-i3-readonly-stage.py", "i14_offline_native_owner_venv")
    pin = helper("v0250-i2-native-preflight.py", "i14_verified_native_wheel")
    require(installer.REPO == installed.REPO == REPO, "source checkout provenance drift")
    native_wheel = REPO / "native/windows/target/wheels" / pin.NATIVE_WHEEL_NAME
    pin.verify_native_wheel(native_wheel)
    sid = storage._sid()
    PHASE = "create-two-private-Connector-Owner-slots"
    for core, connector, _old, _port, _host in ROUTES:
        storage.prepare("cloudflare", core, connector)
        base = storage.root(core)
        storage.check_private(base / "build", directory=True, sid=sid)
        installer._new_dir(base / "build" / SLOT, sid)
        installer._new_dir(base / "services/cloudflare/bin", sid)
    PHASE = "build-two-native-offline-Connector-venvs-and-pinned-binaries"
    digest = helper("v0250-i7-nonprod-core-stage.py", "i14_installed_binary_digest")
    for core, _connector, _old, _port, _host in ROUTES:
        base = storage.root(core)
        build = base / "build" / SLOT
        wheel = installer._wheel(
            REPO,
            build,
            "serverfs_mcp-*.whl",
            (
                "windows_v25/cli.py",
                "windows_v25/runtime.py",
                "windows_v25/isolation.py",
                "windows_v25/readiness.py",
                "windows_v25/storage.py",
                "windows_v25/spec.py",
            ),
        )
        installer._protect_tree(build, sid)
        installed._installed_core(
            installer,
            sid=sid,
            venv=base / "envs/cloudflare",
            wheels=[wheel, native_wheel],
        )
        destination = base / "services/cloudflare/bin/cloudflared.exe"
        require(not destination.exists() and not destination.is_symlink(), "binary occupied")
        shutil.copyfile(binary, destination)
        installer._protect_tree(destination.parent, sid)
        require(digest.sha(destination) == digest.CLOUDFLARED_SHA256, "binary pin drift")
    print("I14_TWO_INDEPENDENT_PRIVATE_NATIVE_CLOUDFLARE_VENVS=PASS")
    PHASE = "copy-two-private-independent-nonproduction-Tokens"
    for core, connector, old, _port, _host in ROUTES:
        source = storage.home("cloudflare", "i7-public", old) / i7.TOKEN_NAME
        destination = storage.home("cloudflare", core, connector) / i7.TOKEN_NAME
        digest, account, tunnel = i7.private_token_record(source, sid=sid)
        data = source.read_bytes()
        installer._new_private_file(destination, data, sid)
        del data
        require(
            i7.private_token_record(destination, sid=sid) == (digest, account, tunnel),
            "private copied nonproduction token identity differs",
        )
    print("I14_TWO_PRIVATE_DISTINCT_NONPROD_TOKENS=PASS")
    PHASE = "register-only-two-disabled-exact-native-connectors"
    for core, connector, _old, port, _host in ROUTES:
        base = storage.root(core)
        manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "cloudflare",
                "instance_id": core,
                "connector_id": connector,
                "sid": sid,
                "pythonw": str(base / "envs/cloudflare/Scripts/pythonw.exe"),
                "settings": {
                    "cloudflared": str(base / "services/cloudflare/bin/cloudflared.exe"),
                    "token_file": str(storage.home("cloudflare", core, connector) / i7.TOKEN_NAME),
                    "port": port,
                    "hostname": core + ".local",
                },
            }
        )
        require(
            manifest.task_name() == InstanceIdentity(core).windows_task("cloudflare", connector),
            "native task identity mismatch",
        )
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        storage.stage(manifest)
        result = Scheduler().install(manifest)
        require(
            result.get("registered") is True
            and result.get("enabled") is False
            and result.get("running") is False,
            "native task was not registered Disabled",
        )
    postflight()
    print("I14_TWO_INDEPENDENT_OWNED_REPO_PUBLIC_CONNECTORS_DISABLED_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    i13, i7, pre, _owner, _binary = baseline()
    require(
        {row["name"] for row in pre.task_rows()} == pre.TASK_NAMES | pre.OPTIONAL_CONNECTOR_NAMES,
        "native Scheduler inventory not exactly four Repo owners plus two Connectors",
    )
    scheduler = Scheduler()
    digest = helper("v0250-i7-nonprod-core-stage.py", "i14_post_binary_digest")
    for core, connector, old, port, _host in ROUTES:
        manifest = storage.load("cloudflare", core, connector)
        base = storage.root(core)
        home = storage.home("cloudflare", core, connector)
        token = home / i7.TOKEN_NAME
        original = storage.home("cloudflare", "i7-public", old) / i7.TOKEN_NAME
        require(
            manifest.component == "cloudflare"
            and manifest.instance_id == core
            and manifest.connector_id == connector
            and manifest.pythonw == str(base / "envs/cloudflare/Scripts/pythonw.exe")
            and manifest.settings["cloudflared"]
            == str(base / "services/cloudflare/bin/cloudflared.exe")
            and manifest.settings["port"] == port
            and manifest.settings["hostname"] == core + ".local"
            and manifest.settings["token_file"] == str(token)
            and i7.private_token_record(token, sid=storage._sid())
            == i7.private_token_record(original, sid=storage._sid()),
            "Connector's nonproduction identity or copied token changed",
        )
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("enabled") is False
            and state.get("state") == "Disabled"
            and state.get("running") is False
            and state.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I14 Connector must be Disabled/offline",
        )
        for name in ("service.json", "task.xml", i7.TOKEN_NAME):
            storage.check_private(home / name, directory=False, sid=storage._sid())
        binary = base / "services/cloudflare/bin/cloudflared.exe"
        storage.check_private(binary, directory=False, sid=storage._sid())
        require(digest.sha(binary) == digest.CLOUDFLARED_SHA256, "pinned binary changed")
        env = base / "envs/cloudflare"
        storage.check_private(env, directory=True, sid=storage._sid())
        for relative in (
            "cli.py",
            "runtime.py",
            "isolation.py",
            "readiness.py",
            "storage.py",
            "spec.py",
        ):
            target = env / "Lib/site-packages/serverfs_mcp/windows_v25" / relative
            source = REPO / "src/serverfs_mcp/windows_v25" / relative
            storage.check_private(target, directory=False, sid=storage._sid())
            require(target.read_bytes() == source.read_bytes(), "installed Owner source drift")
    require(set(i13.IDS) == {r[0] for r in ROUTES}, "Core identity drift")
    print("I14_TWO_CONNECTORS_INDEPENDENT_OWNED_CORES=PASS")
    print("I14_PRIVATE_DISABLED_CONNECTOR_POSTFLIGHT=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--audit", action="store_true")
    options.add_argument("--stage", action="store_true")
    options.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    if args.audit:
        fresh()
        print("I14_READONLY_STAGE_PREFLIGHT=PASS")
    elif args.stage:
        stage()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - return only bounded phase and class
        print(
            "I14_OWNED_REPO_CONNECTOR_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
