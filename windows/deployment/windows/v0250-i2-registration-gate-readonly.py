"""Read-only differential diagnosis for a partially staged Windows I2 Bridge.

This probe intentionally validates the exact owner and overlap gates that
precede the private manifest publication step, without publishing or modifying a manifest. It may
read private Bridge config/grant documents ONLY inside the existing verified
owner validators; no content, paths, SIDs, secrets or exceptions are emitted.
"""

from __future__ import annotations

import ctypes
import os
import sys

INSTANCE = "i2-smoke"
PHASE = "entry"


class I2GateError(RuntimeError):
    """Sanitized owner-only read-only gate refusal."""


def require(value: bool) -> None:
    if not value:
        raise I2GateError("I2 read-only gate not satisfied")


def diagnose() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.bridge_v2_owner import (
        load_native_bridge_plan,
        load_owned_bridge_workdir_claims,
    )
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation
    from serverfs_mcp.windows_v25.spec import Manifest

    PHASE = "native-owner"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin()
    )
    sid = current_user_sid()
    root = storage.root(INSTANCE)
    owner = storage.home("bridge", INSTANCE)
    manifest_path = owner / "service.json"
    task_xml = owner / "task.xml"
    PHASE = "fresh-publish-targets"
    require(
        not manifest_path.exists()
        and not manifest_path.is_symlink()
        and not task_xml.exists()
        and not task_xml.is_symlink()
    )
    print("I2_GATE_MANIFEST_AND_XML=ABSENT")
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": INSTANCE,
            "component": "bridge",
            "sid": sid,
            "pythonw": str(root / "envs" / "bridge" / "Scripts" / "pythonw.exe"),
            "settings": {
                "config": str(owner / "bridge.json"),
                "bridge_python": str(root / "envs" / "agent-bridge" / "Scripts" / "python.exe"),
            },
        }
    )

    PHASE = "owner-plan"
    try:
        load_native_bridge_plan(manifest)
    except Exception as exc:  # noqa: BLE001 - no private context
        print("I2_GATE_OWNER_PLAN=FAILED type=" + type(exc).__name__)
        raise I2GateError("private owner-plan gate rejected") from None
    print("I2_GATE_OWNER_PLAN=PASS")

    PHASE = "per-peer-claim-differential"
    try:
        load_owned_bridge_workdir_claims(manifest)
        print("I2_GATE_PROPOSED_CLAIMS=PASS")
    except Exception as exc:  # noqa: BLE001
        print("I2_GATE_PROPOSED_CLAIMS=FAILED type=" + type(exc).__name__)
        raise I2GateError("proposed Bridge workdir claim rejected") from None
    try:
        installed_peers = storage.component_inventory("bridge")
    except Exception as exc:  # noqa: BLE001
        print("I2_GATE_PEER_INVENTORY=FAILED type=" + type(exc).__name__)
        raise I2GateError("installed Bridge peer inventory rejected") from None
    print("I2_GATE_PEER_INVENTORY=PASS")
    seen_default = False
    for peer in installed_peers:
        if peer.instance_id == INSTANCE:
            continue
        label = "DEFAULT" if peer.instance_id == "default" else "NONDEFAULT"
        seen_default = seen_default or label == "DEFAULT"
        try:
            load_owned_bridge_workdir_claims(peer)
            print("I2_GATE_PEER_" + label + "=PASS")
        except Exception as exc:  # noqa: BLE001
            print("I2_GATE_PEER_" + label + "=FAILED type=" + type(exc).__name__)
            if label == "DEFAULT":
                from pathlib import Path

                from serverfs_mcp.windows_v25 import storage as private_storage

                config = Path(str(peer.settings["config"]))
                repo = Path(__file__).resolve().parents[2]
                scope = (
                    "CHECKOUT"
                    if config.is_relative_to(repo)
                    else "V25_ROOT"
                    if config.is_relative_to(private_storage.root())
                    else "OTHER"
                )
                print("I2_GATE_DEFAULT_CONFIG_SCOPE=" + scope)
                if not config.exists() or config.is_symlink():
                    print("I2_GATE_DEFAULT_CONFIG=ABSENT_OR_REDIRECTED")
                else:
                    try:
                        private_storage.check_private(config, directory=False, sid=peer.sid)
                    except Exception as config_exc:  # noqa: BLE001
                        print("I2_GATE_DEFAULT_CONFIG=UNVERIFIED type=" + type(config_exc).__name__)
                    else:
                        print("I2_GATE_DEFAULT_CONFIG=PRIVATE")
            raise I2GateError("installed peer Bridge workdir claim rejected") from None
    if not seen_default:
        print("I2_GATE_PEER_DEFAULT=ABSENT")

    PHASE = "workdir-isolation"
    try:
        _assert_instance_isolation(manifest)
    except Exception as exc:  # noqa: BLE001
        print("I2_GATE_ISOLATION=FAILED type=" + type(exc).__name__)
        raise I2GateError("Bridge workdir isolation gate rejected") from None
    print("I2_GATE_ISOLATION=PASS")

    PHASE = "storage-preconditions"
    try:
        storage.check_private(root, directory=True, sid=sid)
        storage.check_private(owner, directory=True, sid=sid)
        storage.check_private(
            root / "envs" / "bridge" / "Scripts" / "pythonw.exe",
            directory=False,
            sid=sid,
        )
        # Exact protected ancestors used internally by storage._private_home.
        for entry in (
            storage.root(),
            storage.root() / "instances",
            root / "services",
        ):
            storage.check_private(entry, directory=True, sid=sid)
    except Exception as exc:  # noqa: BLE001
        print("I2_GATE_STORAGE_PRECONDITIONS=FAILED type=" + type(exc).__name__)
        raise I2GateError("manifest storage prerequisites rejected") from None
    print("I2_GATE_STORAGE_PRECONDITIONS=PASS")
    print("I2_GATE_READONLY=PASS")


if __name__ == "__main__":
    try:
        diagnose()
    except Exception as exc:  # noqa: BLE001
        print(
            "I2_GATE_READONLY=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
