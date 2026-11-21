"""Read-only Windows I2 after a failed create-only native Bridge stage.

Inspect exact protected I2 artifacts and fixed Task Scheduler identities.
Never read Bridge JSON/config/grants/task-XML contents, issue a credential,
install/retry/repair a task, or change any ACL or service state.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

INSTANCE = "i2-smoke"
STAGE = "entry"


class I2PostfailError(RuntimeError):
    """Sanitized read-only host evidence refusal."""


def require(value: bool, reason: str) -> None:
    if not value:
        raise I2PostfailError(reason)


def _load_gate():
    source = Path(__file__).with_name("v0250-i2-native-preflight.py")
    spec = importlib.util.spec_from_file_location("i2_postfail_source_gate", source)
    require(spec is not None and spec.loader is not None, "I2 source gate is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.INSTANCE == INSTANCE, "I2 fixture identity differs")
    return module


def classify_private(storage, path: Path, *, directory: bool, sid: str) -> str:
    """Return only an allowlisted class, never a path, SID, or SDDL."""
    try:
        if not path.exists() or path.is_symlink():
            return "ABSENT"
        storage.check_private(path, directory=directory, sid=sid)
        return "PRIVATE"
    except Exception:  # noqa: BLE001 - nonsecret category, no raw diagnostic
        return "UNVERIFIED"


def classify_installed_code(storage, installed: Path, source: Path, *, sid: str) -> str:
    """Compare only Python *source code*, not config/grant private values."""
    try:
        from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

        for item in (installed, source):
            _no_reparse_components(item)
            if not item.is_file() or item.is_symlink():
                return "UNVERIFIED"
        storage.check_private(installed, directory=False, sid=sid)
        with installed.open("rb") as handle:
            installed_digest = hashlib.file_digest(handle, "sha256").digest()
        with source.open("rb") as handle:
            source_digest = hashlib.file_digest(handle, "sha256").digest()
        return "MATCHES" if installed_digest == source_digest else "DIFFERS"
    except Exception:  # noqa: BLE001 - no paths or content in diagnostics
        return "UNVERIFIED"


def classify_original_wheel(storage, base: Path, repo: Path, *, sid: str) -> str:
    """Check code-only wheel-to-source delta, without touching Bridge secrets."""
    try:
        from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

        output = base / "build" / "core-owner"
        storage.check_private(output, directory=True, sid=sid)
        wheels = tuple(output.glob("serverfs_mcp-*.whl"))
        if len(wheels) != 1:
            return "UNVERIFIED"
        wheel = wheels[0]
        _no_reparse_components(wheel)
        storage.check_private(wheel, directory=False, sid=sid)
        changed: list[str] = []
        count = 0
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if not name.startswith("serverfs_mcp/") or not name.endswith(".py"):
                    continue
                if ".." in PurePosixPath(name).parts:
                    return "UNVERIFIED"
                source = repo / "src" / Path(*PurePosixPath(name).parts)
                _no_reparse_components(source)
                if not source.is_file() or source.is_symlink():
                    return "UNVERIFIED"
                count += 1
                with source.open("rb") as handle:
                    if archive.read(name) != handle.read():
                        changed.append(name)
        if count < 1:
            return "UNVERIFIED"
        if not changed:
            return "MATCHES"
        if changed == ["serverfs_mcp/windows_v25/bridge_v2_owner.py"]:
            return "BRIDGE_OWNER_ONLY"
        return "MULTIPLE"
    except Exception:  # noqa: BLE001 - only fixed categories, no code or paths
        return "UNVERIFIED"


def classify_task(rows: object, task_name: str) -> str:
    """Never adopt a duplicate/unknown/foreign Task Scheduler entry."""
    if not isinstance(rows, list):
        return "UNVERIFIED"
    matching = [row for row in rows if isinstance(row, dict) and row.get("name") == task_name]
    if not matching:
        return "ABSENT"
    if len(matching) != 1:
        return "AMBIGUOUS"
    row = matching[0]
    state, enabled = row.get("state"), row.get("enabled")
    if type(state) is not str or type(enabled) is not bool:
        return "UNVERIFIED"
    if state == "Disabled" and enabled is False:
        return "DISABLED"
    return "UNEXPECTED"


def main() -> None:
    global STAGE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    STAGE = "native-owner"
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "non-elevated native Windows Python -I -B required",
    )
    sid = current_user_sid()
    base = storage.root(INSTANCE)
    owner = storage.home("bridge", INSTANCE)
    artifacts = {
        "V25_ROOT": (storage.root(), True),
        "INSTANCES_PARENT": (storage.root() / "instances", True),
        "ROOT": (base, True),
        "SERVICES_PARENT": (base / "services", True),
        "ENVS_PARENT": (base / "envs", True),
        "BRIDGE_VENV": (base / "envs" / "bridge", True),
        "OWNER_HOME": (owner, True),
        "OWNER_STATE": (owner / "state", True),
        "OWNER_LOCKS": (owner / "locks", True),
        "OWNER_PYTHON": (base / "envs" / "bridge" / "Scripts" / "python.exe", False),
        "OWNER_PYTHONW": (base / "envs" / "bridge" / "Scripts" / "pythonw.exe", False),
        "BRIDGE_CONFIG": (owner / "bridge.json", False),
        "PRIVATE_GRANTS": (owner / "state" / "core-grants.json", False),
        "SERVICE_MANIFEST": (owner / "service.json", False),
        "TASK_XML": (owner / "task.xml", False),
    }
    STAGE = "artifact-metadata"
    for label, (path, directory) in artifacts.items():
        print(
            "I2_POSTFAIL_"
            + label
            + "="
            + classify_private(storage, path, directory=directory, sid=sid)
        )

    manifest = owner / "service.json"
    if manifest.exists() and not manifest.is_symlink():
        try:
            storage.load("bridge", INSTANCE)  # service.json only, never Bridge secrets
            print("I2_POSTFAIL_SERVICE_MANIFEST_LOAD=VALID")
        except Exception:  # noqa: BLE001
            print("I2_POSTFAIL_SERVICE_MANIFEST_LOAD=UNVERIFIED")
    else:
        print("I2_POSTFAIL_SERVICE_MANIFEST_LOAD=ABSENT")

    STAGE = "installed-code-identity"
    module_path = Path("serverfs_mcp") / "windows_v25" / "bridge_v2_owner.py"
    installed = base / "envs" / "bridge" / "Lib" / "site-packages" / module_path
    source = Path(__file__).resolve().parents[2] / "src" / module_path
    print(
        "I2_POSTFAIL_INSTALLED_BRIDGE_OWNER="
        + classify_installed_code(storage, installed, source, sid=sid)
    )
    print(
        "I2_POSTFAIL_WHEEL_CODE_DELTA="
        + classify_original_wheel(storage, base, Path(__file__).resolve().parents[2], sid=sid)
    )

    STAGE = "scheduler-inventory"
    gate = _load_gate()
    rows = gate._scheduler_inventory()
    require(isinstance(rows, list), "I2 Scheduler read-only inventory invalid")
    production = [
        row for row in rows if isinstance(row, dict) and row.get("name") in gate.PRODUCTION
    ]
    try:
        gate.verify_scheduler_rows(production)
    except Exception:  # noqa: BLE001
        print("I2_POSTFAIL_PRODUCTION_TASKS=UNVERIFIED")
        raise I2PostfailError("production task baseline is not verifiable") from None
    print("I2_POSTFAIL_PRODUCTION_TASKS=EXPECTED")
    print("I2_POSTFAIL_BRIDGE_TASK=" + classify_task(rows, gate.TASK))
    print("I2_POSTFAIL_CORE_TASK=" + classify_task(rows, "ServerFS v25 i2-smoke Core"))
    STAGE = "bridge-inventory"
    try:
        storage.component_inventory("bridge")  # protected nonsecret service manifests only
        print("I2_POSTFAIL_BRIDGE_INVENTORY=VALID")
    except Exception:  # noqa: BLE001
        print("I2_POSTFAIL_BRIDGE_INVENTORY=UNVERIFIED")
    print("I2_POSTFAIL_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(
            "I2_POSTFAIL_READONLY=FAILED phase=" + STAGE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
