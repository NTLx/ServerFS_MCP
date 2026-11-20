"""One-use nonproduction I5 real Cloudflare Connector OWNER stage.

Prepare TWO genuinely Core-bound Windows v25 Connectors for existing I5
Core A/B sharing the I5 Bridge. Temporarily reuses two pre-existing,
separately verified nonproduction I7 Tunnel credentials by copying their
bytes IN MEMORY under the SAME Windows user SID into NEW private I5 homes.
NO token appears in prompts, output, source, repo files or workdirs.

Both I7 Tunnel owners MUST remain Disabled/offline. Both new I5 owners are
registered Disabled only. Never start a task or mutate Cloudflare/DNS here.
No production, I1-I4, I6/I7 or macOS/Linux lifecycle changes. A real public
canary must enforce mutual exclusion: old I7 and new I5 may NEVER be live
against one Tunnel simultaneously. This stage is nonproduction handover,
not a general persistent multi-owner tunnel-leasing implementation.

One-use: preserve any partial state on failure, never automatically retry.
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
CORES = (
    ("i5-core-a", "i5-edge-a", "i7-edge-a", 18873),
    ("i5-core-b", "i5-edge-b", "i7-edge-b", 18874),
)
SLOT = "i5-public-cloudflare-owner-1"
PHASE = "entry"


class I5PublicConnectorError(RuntimeError):
    """Stable refusal without filesystem, token, command, or SID disclosure."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5PublicConnectorError(reason)


def helper(name: str, alias: str):
    source = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(alias, source)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


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
        "native unprivileged Windows -I -B SID required",
    )


def gates():
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    i7 = helper("v0250-i7-connectors-disabled-register.py", "i5_nonprod_old_i7_tokens")
    i5 = helper("v0250-i5-provider-shared-preflight.py", "i5_nonprod_three_owner_gate")
    require(
        i7.INSTANCE == "i7-public" and i7.IDS == ("i7-edge-a", "i7-edge-b"), "I7 identity drift"
    )
    require(
        i5.BRIDGE == "i5-shared" and set(i5.CORE_PORTS) == {row[0] for row in CORES},
        "I5 identity drift",
    )
    i7.postflight()
    i5.protected_tasks()
    scheduler = Scheduler()
    for core, _connector, original, port in CORES:
        manifest = storage.load("core", core)
        require(
            manifest.component == "core"
            and manifest.instance_id == core
            and manifest.settings["port"] == port
            and manifest.settings["hostname"] == core + ".local",
            "I5 Core manifest not pinned",
        )
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("state") == "Disabled"
            and state.get("enabled") is False
            and state.get("running") is False
            and _current_identity(manifest) is None,
            "I5 Core not Disabled/offline",
        )
        token = storage.home("cloudflare", "i7-public", original) / i7.TOKEN_NAME
        digest, _account, tunnel = i7.private_token_record(token, sid=storage._sid())
        require(
            tunnel == i7.EXPECTED_TUNNELS[original] and len(digest) == 32,
            "I7 Tunnel token mismatch",
        )
    bridge = storage.load("bridge", i5.BRIDGE)
    state = scheduler.status(bridge)
    require(
        state.get("registered") is True
        and state.get("state") == "Disabled"
        and state.get("enabled") is False
        and state.get("running") is False
        and _current_identity(bridge) is None,
        "I5 Bridge not Disabled/offline",
    )
    return i7, i5


def candidate_absent() -> None:
    from serverfs_mcp.windows_v25 import storage

    for core, connector, _original, _port in CORES:
        base = storage.root(core)
        for path in (
            base / "services/cloudflare",
            base / "envs/cloudflare",
            storage.home("cloudflare", core, connector),
        ):
            require(
                not path.exists() and not path.is_symlink(), "private I5 Connector slot occupied"
            )
    i5 = helper("v0250-i5-provider-shared-preflight.py", "i5_new_conn_absent_gate")
    i5.verify_candidates(i5.candidates(), installed=True)
    require(
        set(row["name"] for row in i5.candidates()) == set(i5.TARGETS),
        "I5 named Connector task already exists",
    )


def pinned_binary() -> Path:
    from serverfs_mcp.windows_v25 import storage

    source = storage.root("i7-public") / "services/cloudflare/bin/cloudflared.exe"
    storage.check_private(source, directory=False, sid=storage._sid())
    i7 = helper("v0250-i7-nonprod-core-stage.py", "i5_public_i7_binary")
    require(i7.sha(source) == i7.CLOUDFLARED_SHA256, "nonprod cloudflared binary pin drift")
    return source


def provision() -> None:
    global PHASE
    from serverfs_mcp.instance_identity import InstanceIdentity
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    i7, i5 = gates()
    candidate_absent()
    source_binary = pinned_binary()
    sid = storage._sid()
    installer = helper("v0250-i2-stage.py", "i5_connector_offline_installer")
    native_pin = helper("v0250-i2-native-preflight.py", "i5_connector_native_pin")
    i3 = helper("v0250-i3-readonly-stage.py", "i5_connector_private_venv")
    require(installer.REPO == i3.REPO == REPO, "checkout provenance changed")
    native_wheel = REPO / "native/windows/target/wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native_wheel)

    PHASE = "create-only-two-private-Connector-owners"
    for core, connector, _original, _port in CORES:
        storage.prepare("cloudflare", core, connector)
        base = storage.root(core)
        build = base / "build"
        if build.exists() and not build.is_symlink():
            storage.check_private(build, directory=True, sid=sid)
        else:
            installer._new_dir(build, sid)
        installer._new_dir(build / SLOT, sid)
        installer._new_dir(base / "services/cloudflare/bin", sid)
    print("I5_PUBLIC_TWO_PRIVATE_CONNECTOR_SLOTS=PASS")

    PHASE = "build-two-independent-offline-owner-venvs"
    for core, _connector, _original, _port in CORES:
        base = storage.root(core)
        dest = base / "build" / SLOT
        wheel = installer._wheel(
            REPO,
            dest,
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
        installer._protect_tree(dest, sid)
        i3._installed_core(
            installer,
            sid=sid,
            venv=base / "envs/cloudflare",
            wheels=[wheel, native_wheel],
        )
        binfile = base / "services/cloudflare/bin/cloudflared.exe"
        require(
            not binfile.exists() and not binfile.is_symlink(), "pinned executable already exists"
        )
        shutil.copyfile(source_binary, binfile)
        installer._protect_tree(binfile.parent, sid)
        i7binary = helper("v0250-i7-nonprod-core-stage.py", "i5_bin_verify")
        require(i7binary.sha(binfile) == i7binary.CLOUDFLARED_SHA256, "copied binary pin differs")
    print("I5_PUBLIC_TWO_OFFLINE_CLOUDFLARE_VENVS=PASS")

    PHASE = "copy-two-independent-SID-private-nonproduction-Tokens"
    for core, connector, old_name, _port in CORES:
        source = storage.home("cloudflare", "i7-public", old_name) / i7.TOKEN_NAME
        digest, account, uuid = i7.private_token_record(source, sid=sid)
        require(uuid == i7.EXPECTED_TUNNELS[old_name], "old Tunnel identity differs")
        data = source.read_bytes()
        target = storage.home("cloudflare", core, connector) / i7.TOKEN_NAME
        installer._new_private_file(target, data, sid)
        del data
        new_digest, new_account, new_uuid = i7.private_token_record(target, sid=sid)
        require(
            digest == new_digest and account == new_account and uuid == new_uuid,
            "new private Tunnel token does not match pinned I7 original",
        )
    print("I5_PUBLIC_TWO_DISTINCT_TUNNEL_IDENTITIES_PRIVATE=PASS")

    PHASE = "register-two-Core-bound-disabled-manifests"
    pending = []
    for core, connector, _old_name, port in CORES:
        base = storage.root(core)
        name = core + ".local"
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
                    "hostname": name,
                },
            }
        )
        require(
            manifest.task_name() == InstanceIdentity(core).windows_task("cloudflare", connector),
            "Core-bound Windows native Connector name differs",
        )
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        pending.append(manifest)
    require(len({m.task_name() for m in pending}) == 2, "Connector Scheduler identities overlap")
    for manifest in pending:
        storage.stage(manifest)
        created = Scheduler().install(manifest)
        require(
            created.get("registered") is True
            and created.get("enabled") is False
            and created.get("running") is False,
            "I5 Connector was not registered Disabled",
        )
    print("I5_PUBLIC_TWO_DISABLED_CORE_BOUND_TASKS=PASS")

    PHASE = "native-postflight"
    postflight()
    print("I5_PUBLIC_NONPROD_CONNECTOR_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    i7, i5 = gates()
    sid = storage._sid()
    expected = tuple(InstanceTask for InstanceTask in i5.CONNECTOR_TARGETS)
    i5.verify_candidates(i5.candidates(), installed=True)
    require(
        set(row["name"] for row in i5.candidates()) == set(i5.TARGETS) | set(expected),
        "not exactly two I5 Connector tasks registered",
    )
    bsource = pinned_binary()
    i7bin = helper("v0250-i7-nonprod-core-stage.py", "i5_public_binary_post")
    for core, connector, old_name, port in CORES:
        base = storage.root(core)
        manifest = storage.load("cloudflare", core, connector)
        require(
            manifest.instance_id == core
            and manifest.connector_id == connector
            and manifest.settings["port"] == port
            and manifest.settings["hostname"] == core + ".local"
            and manifest.settings["cloudflared"]
            == str(base / "services/cloudflare/bin/cloudflared.exe")
            and manifest.settings["token_file"]
            == str(storage.home("cloudflare", core, connector) / i7.TOKEN_NAME),
            "I5 Connector identity/host/port/token path drift",
        )
        status = Scheduler().status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("state") == "Disabled"
            and status.get("running") is False
            and status.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I5 Connector not Disabled/offline",
        )
        binfile = Path(str(manifest.settings["cloudflared"]))
        storage.check_private(binfile, directory=False, sid=sid)
        require(
            i7bin.sha(binfile) == i7bin.sha(bsource) == i7bin.CLOUDFLARED_SHA256,
            "I5 Connector binary does not match pinned original",
        )
        token = Path(str(manifest.settings["token_file"]))
        old = storage.home("cloudflare", "i7-public", old_name) / i7.TOKEN_NAME
        left = i7.private_token_record(token, sid=sid)
        right = i7.private_token_record(old, sid=sid)
        require(
            left == right and left[2] == i7.EXPECTED_TUNNELS[old_name],
            "Tunnel resource binding drift",
        )
        env = base / "envs/cloudflare"
        storage.check_private(env, directory=True, sid=sid)
        for rel in (
            "cli.py",
            "runtime.py",
            "isolation.py",
            "readiness.py",
            "storage.py",
            "spec.py",
        ):
            dest = env / "Lib/site-packages/serverfs_mcp/windows_v25" / rel
            src = REPO / "src/serverfs_mcp/windows_v25" / rel
            storage.check_private(dest, directory=False, sid=sid)
            require(
                dest.read_bytes() == src.read_bytes(), "installed Connector module parity differs"
            )
    print("I5_PUBLIC_TWO_DISJOINT_TOKEN_PATHS_AND_IDS=PASS")
    print("I5_PUBLIC_TWO_CORE_BOUND_DISABLED_OWNERS=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--stage", action="store_true")
    group.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    PHASE = "native-i5-i7-protected-baseline"
    if args.postflight:
        native()
        postflight()
        print("I5_PUBLIC_CONNECTOR_POSTFLIGHT=PASS")
    elif args.audit:
        gates()
        candidate_absent()
        pinned_binary()
        print("I5_PUBLIC_CONNECTOR_AUDIT=PASS")
    else:
        provision()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only phase and class escape
        print(
            "I5_PUBLIC_CONNECTOR=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
