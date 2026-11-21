"""I7: create-only isolated Windows nonproduction file-only Core + cloudflared binary.

Audit, single-use stage and postflight. No Cloudflare token, tunnel, named
Connector, DNS, Provider or production-service mutation. The private
cloudflared copy is NOT registered or started; public ingress remains OPEN.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

INSTANCE = "i7-public"
HOST = "i7-public.local"
PORT = 18875
CONNECTORS = ("i7-edge-a", "i7-edge-b")
CLOUDFLARED_SHA256 = "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"
REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"


class I7Refusal(RuntimeError):
    """Stable sanitized failure, never expose private paths or credentials."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I7Refusal(reason)


def helper(name: str, label: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(label, path)
    require(spec is not None and spec.loader is not None, "required protected helper absent")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _host() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I7 requires native non-elevated Python -I -B",
    )


def protected_baseline():
    from serverfs_mcp.windows_v25 import storage

    # Both independent installed gates protect I1-I6 and incumbent production.
    i5 = helper("v0250-i5-bridge-postfail-readonly.py", "i7_i5_protected")
    i6 = helper("v0250-i6-named-connectors-postflight-readonly.py", "i7_i6_protected")
    i5.main()
    i6.main()
    prod = storage.load("cloudflare")
    source = Path(str(prod.settings["cloudflared"]))
    storage.check_private(source, directory=False, sid=prod.sid)
    require(sha(source) == CLOUDFLARED_SHA256, "production executable pin differs")
    print("I7_PROTECTED_I1_I6_AND_BINARY_PIN=PASS")
    return source


def _scheduler_absent() -> None:
    candidates = (
        f"ServerFS v25 {INSTANCE} Core",
        *(f"ServerFS v25 {INSTANCE} Cloudflare Connector {x}" for x in CONNECTORS),
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "$rows=@(Get-ScheduledTask -ErrorAction Stop|Where-Object{"
        "$_.TaskPath -eq '\\' -and $_.TaskName -in @("
        + ",".join("'" + name + "'" for name in candidates)
        + ")});"
        "if($rows.Count -ne 0){exit 11};[Console]::Out.Write('I7_ABSENT')"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(result.returncode == 0 and result.stdout == b"I7_ABSENT", "I7 candidate tasks occupied")


def audit() -> Path:
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    _host()
    source = protected_baseline()
    _scheduler_absent()
    base = storage.root(INSTANCE)
    require(
        not base.exists() and not base.is_symlink(),
        "I7 one-use private instance root already occupied",
    )
    require(
        all(listener.port != PORT for listener in _windows_ipv4_tcp_listeners()),
        "I7 Core loopback port occupied",
    )
    print("I7_NEW_CORE_AND_CONNECTOR_IDENTITIES=ABSENT")
    print("I7_LOOPBACK_PORT_18875=FREE")
    print("I7_READONLY_AUDIT=PASS")
    return source


def _copy_private_binary(source: Path, dest: Path, sid: str) -> None:
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_v25 import storage

    require(not dest.exists() and not dest.is_symlink(), "I7 binary destination occupied")
    with source.open("rb") as inp, dest.open("xb") as out:
        shutil.copyfileobj(inp, out, length=1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    protect_new_file(dest, sid)
    storage.check_private(dest, directory=False, sid=sid)
    require(sha(dest) == CLOUDFLARED_SHA256, "I7 private cloudflared copy differs")


def _policy(fixture: Path) -> bytes:
    # Intentional file-only, strictly read-only and zero Agent/Repo capability.
    return (
        '[server]\nlog_level = "INFO"\n\n'
        "[agent]\nenabled = false\n\n"
        '[[workdirs]]\nalias = "Fixture"\n'
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        'read_only = true\nagent_mode = "disabled"\n'
        "agent_runtimes = []\nrepo_ops_enabled = false\n"
    ).encode("utf-8")


def stage(source: Path) -> None:
    global PHASE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.cli import _assert_instance_isolation, _preflight
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    sid = current_user_sid()
    installer = helper("v0250-i2-stage.py", "i7_offline_helpers")
    i3 = helper("v0250-i3-readonly-stage.py", "i7_core_install_helpers")
    native_pin = helper("v0250-i2-native-preflight.py", "i7_native_pin")
    require(installer.REPO == i3.REPO == REPO, "I7 helper checkout drift")
    native = REPO / "native/windows/target/wheels" / native_pin.NATIVE_WHEEL_NAME
    native_pin.verify_native_wheel(native)

    PHASE = "private-new-core-and-binary-homes"
    storage.prepare("core", INSTANCE)
    storage.prepare("cloudflare", INSTANCE)
    base = storage.root(INSTANCE)
    fixture = base / "workdirs/fixture"
    for directory in (
        base / "workdirs",
        fixture,
        base / "build",
        base / "build/core-owner",
        base / "services/cloudflare/bin",
    ):
        installer._new_dir(directory, sid)
    binary = base / "services/cloudflare/bin/cloudflared.exe"
    _copy_private_binary(source, binary, sid)
    print("I7_PRIVATE_VERIFIED_CLOUDFLARED_COPY=PASS")

    PHASE = "isolated-offline-core-wheel-and-venv"
    wheel = installer._wheel(
        REPO,
        base / "build/core-owner",
        "serverfs_mcp-*.whl",
        (
            "windows_v25/spec.py",
            "windows_v25/runtime.py",
            "windows_v25/cli.py",
            "windows_v25/storage.py",
            "native_config.py",
        ),
    )
    installer._protect_tree(base / "build/core-owner", sid)
    core_python, core_pythonw = i3._installed_core(
        installer,
        sid=sid,
        venv=base / "envs/core",
        wheels=[wheel, native],
    )
    require(core_python.is_file(), "I7 isolated Core Python absent")
    print("I7_OFFLINE_CORE_WHEEL_AND_DACL=PASS")

    PHASE = "private-file-only-policy-and-auth"
    home = storage.home("core", INSTANCE)
    config = home / "serverfs.toml"
    auth = home / "http.env"
    installer._new_private_file(config, _policy(fixture), sid)
    import secrets

    key = secrets.token_urlsafe(48)
    installer._new_private_file(
        auth,
        (
            "SERVERFS_HTTP_API_KEY=" + key + "\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={HOST},127.0.0.1:{PORT}\n"
            "SERVERFS_OAUTH_ENABLED=false\n"
        ).encode("utf-8"),
        sid,
    )
    del key
    manifest = Manifest.parse(
        {
            "schema": 2,
            "instance_id": INSTANCE,
            "component": "core",
            "sid": sid,
            "pythonw": str(core_pythonw),
            "settings": {
                "config": str(config),
                "auth_file": str(auth),
                "port": PORT,
                "hostname": HOST,
            },
        }
    )
    _preflight(manifest)
    _assert_instance_isolation(manifest)
    storage.stage(manifest)
    print("I7_FILE_ONLY_CORE_POLICY_AND_MANIFEST=PASS")

    PHASE = "register-one-disabled-core-only"
    status = Scheduler().install(manifest)
    require(
        status.get("registered") is True
        and status.get("enabled") is False
        and status.get("running") is False,
        "I7 isolated Core registered unexpectedly enabled",
    )
    print("I7_ONLY_CORE_REGISTERED_DISABLED=PASS")
    print("I7_CONNECTOR_TOKENS_AND_TASKS=NOT_CREATED")
    print("I7_STAGE=PASS")


def postflight() -> None:
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.readiness import _current_identity
    from serverfs_mcp.windows_v25.tasks import Scheduler

    _host()
    source = protected_baseline()
    base = storage.root(INSTANCE)
    sid = storage._sid()
    for directory in (
        base,
        base / "workdirs",
        base / "workdirs/fixture",
        base / "services/core",
        base / "services/cloudflare",
        base / "services/cloudflare/bin",
        base / "envs/core",
        base / "build/core-owner",
    ):
        storage.check_private(directory, directory=True, sid=sid)
    binary = base / "services/cloudflare/bin/cloudflared.exe"
    storage.check_private(binary, directory=False, sid=sid)
    require(sha(binary) == sha(source) == CLOUDFLARED_SHA256, "I7 binary copy no longer matches")
    require(
        not (base / "services/cloudflare/connectors").exists()
        and not (base / "envs/cloudflare/Scripts/python.exe").exists(),
        "I7 Connector or runtime unexpectedly installed",
    )
    manifest = storage.load("core", INSTANCE)
    require(
        manifest.instance_id == INSTANCE
        and manifest.settings["port"] == PORT
        and manifest.settings["hostname"] == HOST,
        "I7 manifest identity differs",
    )
    workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
    require(
        not policy.bridge_required
        and len(workdirs) == 1
        and workdirs[0].alias == "Fixture"
        and workdirs[0].read_only is True
        and workdirs[0].root == base / "workdirs/fixture",
        "I7 file-only read-only fixture policy changed",
    )
    from serverfs_mcp.windows_v25.runtime import _core_configuration

    _core_configuration(manifest)
    installed = base / "envs/core/Lib/site-packages/serverfs_mcp"
    for relative in (
        "windows_v25/spec.py",
        "windows_v25/runtime.py",
        "windows_v25/cli.py",
        "windows_v25/storage.py",
        "native_config.py",
    ):
        path = installed / relative
        storage.check_private(path, directory=False, sid=sid)
        require(
            path.read_bytes() == (REPO / "src/serverfs_mcp" / relative).read_bytes(),
            "I7 installed Core bytes differ from source checkpoint",
        )
    expected = {f"ServerFS v25 {INSTANCE} Core"}
    query = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$x=@(Get-ScheduledTask -ErrorAction Stop|Where-Object{"
            "$_.TaskPath -eq '\\' -and $_.TaskName -like 'ServerFS v25 i7-*'});"
            "$x|ForEach-Object{[PSCustomObject]@{name=$_.TaskName;state=[string]$_.State;"
            "enabled=[bool]$_.Settings.Enabled}}|ConvertTo-Json -Compress;"
            "if($x.Count -eq 0){[Console]::Out.Write('[]')}",
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    require(query.returncode == 0, "I7 Scheduler inventory unavailable")
    rows = json.loads(query.stdout.decode("utf-8-sig").strip() or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    require(
        isinstance(rows, list)
        and len(rows) == 1
        and rows[0].get("name") in expected
        and rows[0].get("state") == "Disabled"
        and rows[0].get("enabled") is False,
        "I7 unexpected registered task or enabled process",
    )
    status = Scheduler().status(manifest)
    require(
        status.get("state") == "Disabled"
        and status.get("running") is False
        and status.get("readiness") == "offline"
        and _current_identity(manifest) is None,
        "I7 Core unexpectedly running",
    )
    require(
        all(listener.port != PORT for listener in _windows_ipv4_tcp_listeners()),
        "I7 Core port unexpectedly listening",
    )
    print("I7_PRIVATE_BINARY_SOURCE_AND_CORE_ACL=PASS")
    print("I7_CORE_READ_ONLY_AND_NO_BRIDGE=PASS")
    print("I7_ONE_DISABLED_CORE_NO_CONNECTORS=PASS")
    print("I7_POSTFLIGHT=PASS")


def main() -> None:
    global PHASE

    parser = argparse.ArgumentParser()
    opts = parser.add_mutually_exclusive_group(required=True)
    opts.add_argument("--audit", action="store_true")
    opts.add_argument("--stage", action="store_true")
    opts.add_argument("--postflight", action="store_true")
    args = parser.parse_args()
    if args.postflight:
        PHASE = "postflight"
        postflight()
        return
    PHASE = "preflight"
    source = audit()
    if args.audit:
        return
    PHASE = "stage"
    stage(source)
    PHASE = "postflight"
    postflight()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - stable, nonsecret failure only
        print(
            "I7_STAGE=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
