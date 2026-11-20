"""I10: two independent Cloudflare Connectors into ONE existing I5 Bridge-backed Core.

Windows native, nonadmin, create-only private I10 identity pair. Reuses the
retained I5 Core-A Connector Owner venv and pinned cloudflared executable.
Copies only the two existing distinct I7 *nonproduction* Tunnel Tokens under
the same SID; neither the original I7 owner nor production is changed.
Registered Scheduler entries stay Disabled. No remote config or DNS changes.
The one-use stage must not be repeated after a partial failure.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import sys
from pathlib import Path

CORE = "i5-core-a"
BRIDGE = "i5-shared"
PORT = 18873
CONNECTORS = (("i10-edge-a", "i7-edge-a"), ("i10-edge-b", "i7-edge-b"))
PHASE = "entry"


class I10Refusal(RuntimeError):
    """Stable nonsecret refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I10Refusal(reason)


def helper(name: str, alias: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(alias, path)
    require(spec is not None and spec.loader is not None, "fixed helper missing")
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
        and storage._sid() == current_user_sid(),
        "Windows unprivileged user -I -B required",
    )


def baseline():
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    native()
    existing = helper("v0250-i5-bridge-public-connector-stage.py", "i10_verified_i5_setup")
    i7, i5 = existing.gates()  # verifies production, prior I7 and 3 I5 owners offline
    require(
        CORE in i5.CORE_PORTS
        and i5.CORE_PORTS[CORE] == PORT
        and i5.BRIDGE == BRIDGE
        and i7.IDS == ("i7-edge-a", "i7-edge-b"),
        "I10 baseline identities drifted",
    )
    core = storage.load("core", CORE)
    bridge = storage.load("bridge", BRIDGE)
    scheduler = Scheduler()
    for manifest in (core, bridge):
        state = scheduler.status(manifest)
        require(
            state.get("registered") is True
            and state.get("enabled") is False
            and state.get("state") == "Disabled"
            and state.get("running") is False
            and state.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I5 Core/Bridge fixture not Disabled/offline",
        )
    require(
        core.settings["port"] == PORT and core.settings["hostname"] == CORE + ".local",
        "I5 Core manifest public Host drift",
    )
    owner = storage.root(CORE) / "envs/cloudflare"
    executable = storage.root(CORE) / "services/cloudflare/bin/cloudflared.exe"
    sid = storage._sid()
    for path in (owner / "Scripts/python.exe", owner / "Scripts/pythonw.exe", executable):
        storage.check_private(path, directory=False, sid=sid)
        require(path.is_file() and not path.is_symlink(), "retained I5 owner binary absent")
    i7binary = helper("v0250-i7-nonprod-core-stage.py", "i10_pinned_cloudflared")
    require(
        i7binary.sha(executable) == i7binary.CLOUDFLARED_SHA256,
        "I10 cannot reuse unverified cloudflared",
    )
    return i7, i5, owner, executable


def fresh() -> None:
    from serverfs_mcp.windows_v25 import storage

    i7, i5, _owner, _binary = baseline()
    retired = helper("v0250-i5-public-connector-retire.py", "i10_exact_old_retirement")
    retired.postflight()
    require(
        set(row["name"] for row in i5.candidates()) == set(i5.TARGETS),
        "nonproduction I5 optional Scheduler namespace already occupied",
    )
    for connector, original in CONNECTORS:
        home = storage.home("cloudflare", CORE, connector)
        require(
            not home.exists() and not home.is_symlink(),
            "I10 candidate Connector private home occupied",
        )
        source = storage.home("cloudflare", "i7-public", original) / i7.TOKEN_NAME
        _digest, _account, uuid = i7.private_token_record(source, sid=storage._sid())
        require(
            uuid == i7.EXPECTED_TUNNELS[original],
            "I10 original nonproduction Tunnel token identity differs",
        )


def stage() -> None:
    global PHASE
    from serverfs_mcp.instance_identity import InstanceIdentity
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    fresh()
    i7, _, owner, binary = baseline()
    installer = helper("v0250-i2-stage.py", "i10_private_creator")
    sid = storage._sid()
    PHASE = "create-two-new-SID-private-Connector-identities"
    for connector, original in CONNECTORS:
        storage.prepare("cloudflare", CORE, connector)
        previous = storage.home("cloudflare", "i7-public", original) / i7.TOKEN_NAME
        token = storage.home("cloudflare", CORE, connector) / i7.TOKEN_NAME
        digest, account, uuid = i7.private_token_record(previous, sid=sid)
        data = previous.read_bytes()
        installer._new_private_file(token, data, sid)
        del data
        require(
            i7.private_token_record(token, sid=sid) == (digest, account, uuid),
            "I10 private copied nonprod token differs",
        )
    print("I10_TWO_DISTINCT_PRIVATE_NONPROD_TOKENS=PASS")
    PHASE = "disabled-Connector-registration-only"
    for connector, _old in CONNECTORS:
        manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "cloudflare",
                "instance_id": CORE,
                "connector_id": connector,
                "sid": sid,
                "pythonw": str(owner / "Scripts/pythonw.exe"),
                "settings": {
                    "cloudflared": str(binary),
                    "token_file": str(storage.home("cloudflare", CORE, connector) / i7.TOKEN_NAME),
                    "port": PORT,
                    "hostname": CORE + ".local",
                },
            }
        )
        require(
            manifest.task_name() == InstanceIdentity(CORE).windows_task("cloudflare", connector),
            "I10 task identity drift",
        )
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        storage.stage(manifest)
        result = Scheduler().install(manifest)
        require(
            result.get("registered") is True
            and result.get("enabled") is False
            and result.get("running") is False,
            "I10 registered task not disabled",
        )
    postflight()
    print("I10_TWO_CONNECTORS_ONE_CORE_DISABLED_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    i7, i5, owner, binary = baseline()
    all_i5 = i5.candidates()
    require(
        set(row["name"] for row in all_i5)
        == set(i5.TARGETS)
        | {f"ServerFS v25 {CORE} Cloudflare Connector {name}" for name, _original in CONNECTORS},
        "I10 Scheduler inventory not exactly two extra Connectors",
    )
    i5.verify_candidates(all_i5, installed=True)
    scheduler = Scheduler()
    for connector, original in CONNECTORS:
        manifest = storage.load("cloudflare", CORE, connector)
        token = storage.home("cloudflare", CORE, connector) / i7.TOKEN_NAME
        prior = storage.home("cloudflare", "i7-public", original) / i7.TOKEN_NAME
        require(
            manifest.component == "cloudflare"
            and manifest.instance_id == CORE
            and manifest.connector_id == connector
            and manifest.settings["port"] == PORT
            and manifest.settings["hostname"] == CORE + ".local"
            and manifest.settings["cloudflared"] == str(binary)
            and manifest.settings["token_file"] == str(token)
            and manifest.pythonw == str(owner / "Scripts/pythonw.exe")
            and i7.private_token_record(token, sid=storage._sid())
            == i7.private_token_record(prior, sid=storage._sid()),
            "I10 signed/Tunnel source identity mismatch",
        )
        status = scheduler.status(manifest)
        require(
            status.get("registered") is True
            and status.get("enabled") is False
            and status.get("state") == "Disabled"
            and status.get("running") is False
            and status.get("readiness") == "offline"
            and _current_identity(manifest) is None,
            "I10 Connector not Disabled/offline",
        )
        for name in ("service.json", "task.xml", i7.TOKEN_NAME):
            storage.check_private(
                storage.home("cloudflare", CORE, connector) / name,
                directory=False,
                sid=storage._sid(),
            )
    print("I10_BOTH_CONNECTORS_SAME_CORE_DIFFERENT_TUNNELS=PASS")
    print("I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS")


def main() -> None:
    global PHASE
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--stage", action="store_true")
    group.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    PHASE = "protected-preflight"
    if args.audit:
        fresh()
        print("I10_DISABLED_STAGE_AUDIT=PASS")
    elif args.stage:
        stage()
    else:
        postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - private data never returned
        print(
            "I10_DISABLED_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
