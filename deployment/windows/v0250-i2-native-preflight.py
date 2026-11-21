"""Read-only Windows I2 preflight for a *new* isolated Bridge-v2 owner.

No credentials are read and no process, service, task, ACL or repository state
is mutated. The subsequent installer must independently repeat these gates.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

INSTANCE = "i2-smoke"
TASK = "ServerFS v25 i2-smoke Agent Bridge"
NATIVE_WHEEL_NAME = "serverfs_windows_native-0.24.2-cp312-abi3-win_amd64.whl"
NATIVE_WHEEL_SHA256 = "127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3"
REPO = Path(__file__).resolve().parents[2]
PRODUCTION = {
    "ServerFS Native Tunnel": (True, "Running"),
    "ServerFS v25 Core": (True, "Running"),
    "ServerFS v25 Cloudflare Connector": (True, "Running"),
    "ServerFS v25 Agent Bridge": (False, None),
    "ServerFS v25 OpenAI Connector": (False, None),
}
TARGETS = frozenset({TASK, "ServerFS v25 i2-smoke Core"})
SCRIPT = r"""
$ErrorActionPreference='Stop'
$targets=@(
  'ServerFS Native Tunnel',
  'ServerFS v25 Core',
  'ServerFS v25 Cloudflare Connector',
  'ServerFS v25 Agent Bridge',
  'ServerFS v25 OpenAI Connector',
  'ServerFS v25 i2-smoke Agent Bridge',
  'ServerFS v25 i2-smoke Core'
)
$rows=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $targets -ccontains $_.TaskName
} | ForEach-Object {
  [PSCustomObject]@{
    name=$_.TaskName
    state=[string]$_.State
    enabled=[bool]$_.Settings.Enabled
  }
})
ConvertTo-Json -InputObject $rows -Compress
"""


class I2PreflightError(RuntimeError):
    """Fixed nonsecret Windows I2 preflight failure."""


def verify_scheduler_rows(rows: object) -> None:
    """Refuse duplicates, a changed production baseline, or an occupied I2 ID."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise I2PreflightError("Scheduler inventory is malformed")
    names = [row.get("name") for row in rows]
    if (
        any(type(name) is not str for name in names)
        or len(names) != len(PRODUCTION)
        or len(names) != len(set(names))
    ):
        raise I2PreflightError("Scheduler inventory does not match the protected baseline")
    if any(name in TARGETS for name in names):
        raise I2PreflightError("an I2 task is already registered")
    for name, (expected_enabled, expected_state) in PRODUCTION.items():
        row = next((row for row in rows if row.get("name") == name), None)
        if row is None or type(row.get("enabled")) is not bool:
            raise I2PreflightError("production Scheduler baseline is incomplete")
        if row["enabled"] != expected_enabled:
            raise I2PreflightError("production Scheduler enabled state drifted")
        state = row.get("state")
        if not isinstance(state, str):
            raise I2PreflightError("production Scheduler state is malformed")
        if expected_state is not None and state != expected_state:
            raise I2PreflightError("production Scheduler task is not Running")
        if expected_state is None and state != "Disabled":
            raise I2PreflightError("disabled Bridge or connector state is inconsistent")


def verify_native_wheel(path: Path) -> None:
    """Check the pinned native wheel before any create-only instance mutation."""
    if (
        not isinstance(path, Path)
        or path.name != NATIVE_WHEEL_NAME
        or not isinstance(NATIVE_WHEEL_SHA256, str)
        or len(NATIVE_WHEEL_SHA256) != 64
    ):
        raise I2PreflightError("I2 native Wheel identity is invalid")
    try:
        from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

        _no_reparse_components(path)
        if not path.is_file() or path.is_symlink():
            raise I2PreflightError("I2 pinned native Wheel is missing or redirected")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != NATIVE_WHEEL_SHA256:
            raise I2PreflightError("I2 pinned native Wheel integrity differs")
    except I2PreflightError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise I2PreflightError("I2 pinned native Wheel cannot be validated") from exc


def verify_offline_builder() -> None:
    """Probe only the executable's version; never resolve or install packages."""
    executable = shutil.which("uv")
    if not executable:
        raise I2PreflightError("I2 offline uv build executable is unavailable")
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not result.stdout.startswith(b"uv "):
            raise I2PreflightError("I2 offline uv build tool is unverified")
    except (OSError, subprocess.SubprocessError) as exc:
        raise I2PreflightError("I2 offline uv build tool cannot run") from exc


def _scheduler_inventory() -> object:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise I2PreflightError("read-only Scheduler query is unavailable")
    try:
        return json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise I2PreflightError("Scheduler returned malformed data") from exc


def main() -> None:
    from serverfs_mcp.windows_v25 import storage

    if (
        sys.platform != "win32"
        or os.name != "nt"
        or not sys.flags.isolated
        or not sys.dont_write_bytecode
        or ctypes.windll.shell32.IsUserAnAdmin()
    ):
        raise I2PreflightError("ordinary-user native Windows Python -I -B is required")
    verify_scheduler_rows(_scheduler_inventory())
    print("I2_PRODUCTION_SCHEDULER=EXPECTED")
    base = storage.root(INSTANCE)
    if base.exists() or base.is_symlink():
        raise I2PreflightError("I2 instance root already exists")
    print("I2_NEW_INSTANCE_ROOT=ABSENT")
    if not (REPO / "agent_bridge" / "pyproject.toml").is_file():
        raise I2PreflightError("Bridge worker package source is missing")
    if not (REPO / "src" / "serverfs_mcp" / "windows_v25" / "bridge_v2_owner.py").is_file():
        raise I2PreflightError("native Bridge owner package source is missing")
    print("I2_BRIDGE_SOURCES=PRESENT")
    verify_native_wheel(REPO / "native" / "windows" / "target" / "wheels" / NATIVE_WHEEL_NAME)
    print("I2_PINNED_NATIVE_WHEEL=VERIFIED")
    verify_offline_builder()
    print("I2_UV_OFFLINE_BUILDER=AVAILABLE")
    print("I2_NATIVE_PREFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - redacted fixed failure marker
        print("I2_NATIVE_PREFLIGHT=FAILED category=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
