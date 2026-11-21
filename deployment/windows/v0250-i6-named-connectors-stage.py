"""I6: create-only, Disabled-only two named Cloudflare Connector registrations.

Both are attached to the existing *offline* i4-a test Core. They use
nonfunctional placeholder tunnel material, so starting a real Cloudflare
Tunnel is impossible. No production/legacy Core, Bridge or Connector task
is ever changed. This stage is one-use; a partial failure is not retried.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IDS = ("i6-edge-a", "i6-edge-b")
INSTANCE = "i4-a"
PHASE = "entry"


class I6StageError(RuntimeError):
    """Non-secret refusal; preserve partial state for diagnosis."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I6StageError(reason)


def helper(filename: str, name: str):
    file = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, file)
    require(spec is not None and spec.loader is not None, "I6 fixed helper is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_tasks(*, installed: bool) -> None:
    """Get-ScheduledTask read-only exact identity audit; never /Run or /End."""
    names = tuple(
        f"ServerFS v25 {INSTANCE} Cloudflare Connector {connector_id}" for connector_id in IDS
    )
    assert len(set(names)) == 2
    script = (
        "$ErrorActionPreference='Stop';"
        "$hits=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {"
        "$_.TaskPath -eq '\\' -and $_.TaskName -in @("
        + ",".join("'" + name + "'" for name in names)
        + ") });"
        "$hits | ForEach-Object { "
        "[PSCustomObject]@{ name=$_.TaskName; state=[string]$_.State;"
        "enabled=[bool]$_.Settings.Enabled }} | ConvertTo-Json -Compress;"
        "if($hits.Count -eq 0){[Console]::Out.Write('[]')}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0, "I6 candidate Scheduler query failed")
    try:
        raw = result.stdout.decode("utf-8-sig").strip()
        rows = json.loads(raw or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        require(isinstance(rows, list), "I6 Scheduler records are invalid")
        mapped = {row["name"]: row for row in rows}
        require(len(mapped) == len(rows), "I6 Scheduler identity duplicated")
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise I6StageError("I6 candidate Scheduler inventory is malformed") from exc
    if not installed:
        require(not rows, "I6 candidate task identity is occupied")
    else:
        require(
            set(mapped) == set(names)
            and all(
                mapped[name]["state"] == "Disabled" and mapped[name]["enabled"] is False
                for name in names
            ),
            "I6 named tasks must both remain Disabled",
        )


def preflight() -> None:
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I6 requires native non-admin Python -I -B",
    )
    # This existing native I4 postflight gate checks all production+I1..I4
    # identities and both I4 Core/Bridge pairs Disabled/offline, no listeners.
    gate = helper("v0250-i4-postflight-readonly.py", "i6_i4_postflight")
    gate.main()
    fixture_tasks(installed=False)
    base = storage.root(INSTANCE)
    storage.check_private(base, directory=True)
    for item in (
        base / "services" / "cloudflare",
        base / "envs" / "cloudflare",
        base / "build" / "cloudflare-owner",
    ):
        require(
            not item.exists() and not item.is_symlink(),
            "I6 create-only scope is already occupied",
        )
    print("I6_CANDIDATE_TASKS_ABSENT=PASS")
    print("I6_NAMED_CONNECTOR_PRIVATE_ROOTS_FRESH=PASS")
    print("I6_NATIVE_PREFLIGHT=PASS")


def stage() -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    PHASE = "read-only-preflight"
    preflight()
    sid = current_user_sid()
    installer = helper("v0250-i2-stage.py", "i6_private_helpers")
    i3 = helper("v0250-i3-readonly-stage.py", "i6_install_helpers")
    native_pin = helper("v0250-i2-native-preflight.py", "i6_native_pin")
    require(installer.REPO == i3.REPO == REPO, "I6 installer repo differs")
    native = REPO / "native/windows/target/wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native)

    PHASE = "create-private-homes"
    base = storage.root(INSTANCE)
    for connector_id in IDS:
        storage.prepare("cloudflare", INSTANCE, connector_id)
    installer._new_dir(base / "build" / "cloudflare-owner", sid)
    installer._new_dir(base / "services/cloudflare/bin", sid)
    print("I6_TWO_NAMED_PRIVATE_HOMES=PASS")

    PHASE = "offline-built-and-installed-venv"
    wheel = installer._wheel(
        REPO,
        base / "build/cloudflare-owner",
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
    installer._protect_tree(base / "build/cloudflare-owner", sid)
    py, pyw = i3._installed_core(
        installer,
        sid=sid,
        venv=base / "envs/cloudflare",
        wheels=[wheel, native],
    )
    require(
        (base / "envs/cloudflare/Lib/site-packages/serverfs_mcp/windows_v25/spec.py").read_bytes()
        == (REPO / "src/serverfs_mcp/windows_v25/spec.py").read_bytes(),
        "I6 installed named Connector schema differs",
    )
    print("I6_CLOUDFLARE_OWNER_PRIVATE_VENV=VERIFIED")

    # Deliberately NOT a valid executable or tunnel token. The test can
    # establish Scheduler registration while being unable to open any tunnel.
    PHASE = "nonfunctional-private-ingress-fixtures"
    fake_binary = base / "services/cloudflare/bin/cloudflared.exe"
    installer._new_private_file(fake_binary, b"DISABLED_I6_NON_EXECUTABLE_FIXTURE\n", sid)
    core = storage.load("core", INSTANCE)
    require(
        core.instance_id == INSTANCE
        and core.component == "core"
        and core.settings["port"] == 18871,
        "I6 fixture Core identity changed",
    )
    manifests = []
    for connector_id in IDS:
        home = storage.home("cloudflare", INSTANCE, connector_id)
        token = home / "fixture.token"
        installer._new_private_file(
            token,
            ("DISABLED_I6_FAKE_TOKEN_" + connector_id + "\n").encode("ascii"),
            sid,
        )
        manifest = Manifest.parse(
            {
                "schema": 2,
                "component": "cloudflare",
                "instance_id": INSTANCE,
                "connector_id": connector_id,
                "sid": sid,
                "pythonw": str(pyw),
                "settings": {
                    "cloudflared": str(fake_binary),
                    "token_file": str(token),
                    "port": core.settings["port"],
                    "hostname": core.settings["hostname"],
                },
            }
        )
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        manifests.append(manifest)
    require(len({m.task_name() for m in manifests}) == 2, "I6 task identity collision")
    print("I6_TWO_FAKE_CREDENTIALS_AND_OFFLINE_PREFLIGHT=PASS")

    PHASE = "private-manifests"
    for manifest in manifests:
        storage.stage(manifest)
        loaded = storage.load("cloudflare", INSTANCE, manifest.connector_id)
        require(loaded == manifest, "I6 protected manifest differs")
    require(
        {
            m.connector_id
            for m in storage.component_inventory("cloudflare")
            if m.instance_id == INSTANCE
        }
        == set(IDS),
        "I6 connector inventory is not exact",
    )
    print("I6_NAMED_MANIFESTS_PRIVATE=PASS")

    PHASE = "register-disabled-only"
    scheduler = Scheduler()
    for manifest in manifests:
        status = scheduler.install(manifest)
        require(
            status["registered"] is True
            and status["enabled"] is False
            and status["running"] is False,
            "I6 newly registered task was not Disabled",
        )
        status = scheduler.status(manifest)
        require(
            status["state"] == "Disabled" and status["readiness"] == "offline",
            "I6 newly installed Connector is unexpectedly running",
        )
    fixture_tasks(installed=True)
    # Re-verify all incumbent I4 and production identities after registration.
    helper("v0250-i4-postflight-readonly.py", "i6_after_i4_postflight").main()
    print("I6_TWO_TASKS_REGISTERED_DISABLED=PASS")
    print("I6_PROTECTED_TASKS_UNCHANGED=PASS")
    print("I6_NATIVE_DISABLED_ONLY_STAGE=PASS")


if __name__ == "__main__":
    if str(REPO / "src") not in sys.path:
        # This source checkout is the artifact being staged. The script still
        # requires the isolated interpreter and verifies its built wheel bytes.
        sys.path.insert(0, str(REPO / "src"))
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--preflight":
            preflight()
        elif len(sys.argv) == 2 and sys.argv[1] == "--stage":
            stage()
        else:
            raise I6StageError("I6 only accepts --preflight or --stage")
    except BaseException as exc:
        print(
            "I6_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
