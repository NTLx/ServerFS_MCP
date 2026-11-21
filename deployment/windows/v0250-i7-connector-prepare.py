"""I7 stage-2: two private Connector homes + one offline Windows owner venv.

PREPARE ONLY. NEVER create/read a Tunnel token, register a Connector task,
start Core/cloudflared, modify any existing service or touch Cloudflare API.
Phase 1 I7 installed Core must be verified and Disabled. Stage is one-use.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

INSTANCE = "i7-public"
CONNECTORS = ("i7-edge-a", "i7-edge-b")
PHASE = "entry"
REPO = Path(__file__).resolve().parents[2]


class I7ConnectorPrepareRefusal(RuntimeError):
    """Fixed, nonsecret stage-2 refusal."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I7ConnectorPrepareRefusal(reason)


def helper(name: str, label: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "I7 required helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I7 requires native non-elevated Python -I -B",
    )


def candidates_absent() -> None:
    """Never use a wildcard to mutate Scheduler; read candidate IDs only."""
    names = tuple(f"ServerFS v25 {INSTANCE} Cloudflare Connector {name}" for name in CONNECTORS)
    script = (
        "$ErrorActionPreference='Stop';"
        "$hits=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {"
        "$_.TaskPath -eq '\\' -and $_.TaskName -in @("
        + ",".join("'" + name + "'" for name in names)
        + ")});"
        "if($hits.Count -ne 0){exit 13};[Console]::Out.Write('I7_CONNECTORS_ABSENT')"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        result.returncode == 0 and result.stdout == b"I7_CONNECTORS_ABSENT",
        "I7 Connector Scheduler identity occupied",
    )


def _base_core():
    from serverfs_mcp.windows_v25 import storage

    phase1 = helper("v0250-i7-nonprod-core-stage.py", "i7_phase1_gate")
    require(
        phase1.INSTANCE == INSTANCE and phase1.CONNECTORS == CONNECTORS and phase1.PORT == 18875,
        "I7 phase-1 identity changed",
    )
    # Stage-1 postflight deliberately rejects any Connector-prepared state;
    # call only BEFORE phase-2 installation. Phase-2 gets its own postflight.
    phase1.postflight()
    core = storage.load("core", INSTANCE)
    require(
        core.component == "core"
        and core.settings["port"] == 18875
        and core.settings["hostname"] == phase1.HOST,
        "I7 Core target changed",
    )
    return core


def audit() -> None:
    from serverfs_mcp.windows_v25 import storage

    host()
    _base_core()
    candidates_absent()
    base = storage.root(INSTANCE)
    sid = storage._sid()
    venv = base / "envs/cloudflare"
    storage.check_private(venv, directory=True, sid=sid)
    require(
        not any(venv.iterdir()),
        "I7 cloudflare owner venv is not fresh",
    )
    for candidate in (
        base / "build/cloudflare-owner",
        *(storage.home("cloudflare", INSTANCE, name) for name in CONNECTORS),
    ):
        require(
            not candidate.exists() and not candidate.is_symlink(),
            "I7 Connector preparation already occupies private identity",
        )
    print("I7_TWO_NEW_CONNECTOR_HOMES=ABSENT")
    print("I7_CLOUDFLARE_OWNER_VENV=FRESH")
    print("I7_CONNECTOR_PREPARE_AUDIT=PASS")


def _installed_core_and_binary() -> None:
    """Shared parent checks are read-only; phase-2 checks never need tokens."""
    from serverfs_mcp.windows_v25 import storage

    phase1 = helper("v0250-i7-nonprod-core-stage.py", "i7_prep_phase1_protected")
    phase1.protected_baseline()
    base = storage.root(INSTANCE)
    sid = storage._sid()
    core = storage.load("core", INSTANCE)
    require(
        core.component == "core"
        and core.instance_id == INSTANCE
        and core.settings["port"] == phase1.PORT
        and core.settings["hostname"] == phase1.HOST,
        "I7 phase-1 installed Core identity differs",
    )
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    state = Scheduler().status(core)
    require(
        state.get("registered") is True
        and state.get("state") == "Disabled"
        and state.get("enabled") is False
        and state.get("running") is False
        and _current_identity(core) is None,
        "I7 Core is not Disabled/offline",
    )
    require(
        all(listener.port != phase1.PORT for listener in _windows_ipv4_tcp_listeners()),
        "I7 Core port is unexpectedly listening",
    )
    executable = base / "services/cloudflare/bin/cloudflared.exe"
    storage.check_private(executable, directory=False, sid=sid)
    require(
        phase1.sha(executable) == phase1.CLOUDFLARED_SHA256,
        "I7 cloudflared executable pin changed",
    )


def postflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    host()
    _installed_core_and_binary()
    candidates_absent()
    base = storage.root(INSTANCE)
    sid = storage._sid()
    for directory in (
        base / "envs/cloudflare",
        base / "build/cloudflare-owner",
        base / "services/cloudflare/connectors",
        *(storage.home("cloudflare", INSTANCE, name) for name in CONNECTORS),
    ):
        storage.check_private(directory, directory=True, sid=sid)
    homes = base / "services/cloudflare/connectors"
    require(
        {item.name for item in homes.iterdir()} == set(CONNECTORS)
        and all(item.is_dir() and not item.is_symlink() for item in homes.iterdir()),
        "I7 private Connector homes are not exact",
    )
    for name in CONNECTORS:
        home = storage.home("cloudflare", INSTANCE, name)
        require(
            not any(home.iterdir()),
            "I7 staged Connector home must contain no token or manifest",
        )
    from serverfs_mcp.windows_v25 import storage as s
    from serverfs_mcp.windows_v25.spec import Manifest

    require(
        not any(
            isinstance(m, Manifest) and m.instance_id == INSTANCE
            for m in s.component_inventory("cloudflare")
        ),
        "I7 Connector manifests appeared before credentials",
    )
    runtime = base / "envs/cloudflare"
    for executable in ("python.exe", "pythonw.exe"):
        storage.check_private(runtime / "Scripts" / executable, directory=False, sid=sid)
    for relative in (
        "windows_v25/runtime.py",
        "windows_v25/storage.py",
        "windows_v25/cli.py",
        "windows_v25/spec.py",
        "windows_v25/readiness.py",
        "windows_v25/isolation.py",
    ):
        installed = runtime / "Lib/site-packages/serverfs_mcp" / relative
        original = REPO / "src/serverfs_mcp" / relative
        storage.check_private(installed, directory=False, sid=sid)
        require(
            installed.read_bytes() == original.read_bytes(),
            "I7 Connector venv source parity changed",
        )
    print("I7_TWO_CONNECTOR_HOMES_PRIVATE_EMPTY=PASS")
    print("I7_CLOUDFLARE_OWNER_INSTALLED=PASS")
    print("I7_NO_CONNECTOR_TOKENS_MANIFESTS_OR_TASKS=PASS")
    print("I7_CONNECTOR_PREPARE_POSTFLIGHT=PASS")


def stage() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    PHASE = "preflight"
    audit()
    sid = current_user_sid()
    installer = helper("v0250-i2-stage.py", "i7_connector_helpers")
    i3 = helper("v0250-i3-readonly-stage.py", "i7_connector_install_helper")
    native_pin = helper("v0250-i2-native-preflight.py", "i7_connector_native_pin")
    require(
        installer.REPO == i3.REPO == REPO,
        "I7 Connector build helper checkout differs",
    )
    native = REPO / "native/windows/target/wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native)
    PHASE = "new-private-homes"
    for connector in CONNECTORS:
        storage.prepare("cloudflare", INSTANCE, connector)
    output = storage.root(INSTANCE) / "build/cloudflare-owner"
    installer._new_dir(output, sid)
    print("I7_TWO_PRIVATE_CONNECTOR_HOMES_CREATED=PASS")

    PHASE = "offline-source-verified-wheel"
    wheel = installer._wheel(
        REPO,
        output,
        "serverfs_mcp-*.whl",
        (
            "windows_v25/spec.py",
            "windows_v25/storage.py",
            "windows_v25/cli.py",
            "windows_v25/runtime.py",
            "windows_v25/readiness.py",
            "windows_v25/isolation.py",
        ),
    )
    installer._protect_tree(output, sid)
    PHASE = "offline-private-connector-owner-venv"
    i3._installed_core(
        installer,
        sid=sid,
        venv=storage.root(INSTANCE) / "envs/cloudflare",
        wheels=[wheel, native],
    )
    print("I7_CONNECTOR_OWNER_INSTALLED_PRIVATE=PASS")
    PHASE = "postflight"
    postflight()
    print("I7_CONNECTOR_PREPARE_STAGE=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    opts = parser.add_mutually_exclusive_group(required=True)
    opts.add_argument("--audit", action="store_true")
    opts.add_argument("--stage", action="store_true")
    opts.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    if args.postflight:
        postflight()
    elif args.audit:
        audit()
    else:
        stage()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - redact paths, token data
        print(
            "I7_CONNECTOR_PREPARE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
