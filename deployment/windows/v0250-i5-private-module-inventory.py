"""Read-only I5 installed Bridge owner venv package/version-shape inventory.

No service action, source repair, model, credential, raw path or private
config read. Confirm whether this VENV includes the newer Windows v25
legacy-independent-owner coexistence module and CLI preflight exports.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path


class I5PrivateInventoryError(RuntimeError):
    """Only nonsecret failure codes."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5PrivateInventoryError(message)


def main() -> None:
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 private inventory needs native Python -I -B",
    )
    local = os.environ.get("LOCALAPPDATA", "")
    require(bool(local), "I5 native Windows environment unavailable")
    expected = (
        Path(local)
        / "ServerFS"
        / "v25"
        / "instances"
        / "i5-shared"
        / "envs"
        / "bridge"
        / "Scripts"
        / "python.exe"
    )
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True),
        "I5 installed Bridge owner Python is required",
    )

    import serverfs_mcp
    from serverfs_mcp.windows_v25 import cli, runtime

    package = Path(serverfs_mcp.__file__).resolve(strict=True)
    root = Path(sys.prefix).resolve(strict=True)
    require(package.is_relative_to(root), "I5 package was loaded outside its own private venv")
    print("I5_PRIVATE_MODULE_PACKAGE=INSTALLED")
    print(
        "I5_PRIVATE_LEGACY_COEXISTENCE_MODULE="
        + (
            "PRESENT"
            if importlib.util.find_spec("serverfs_mcp.windows_v25.legacy_bridge_ownership")
            else "ABSENT"
        )
    )
    for name in ("_preflight", "_assert_instance_isolation"):
        print(
            "I5_PRIVATE_CLI_"
            + ("PREFLIGHT" if name == "_preflight" else "ISOLATION")
            + "="
            + ("PRESENT" if callable(getattr(cli, name, None)) else "ABSENT")
        )
    print(
        "I5_PRIVATE_BRIDGE_V2_ENTRY="
        + ("PRESENT" if callable(getattr(runtime, "run_independent_bridge_v2", None)) else "ABSENT")
    )
    print("I5_PRIVATE_MODULE_READONLY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - only safe class name
        print("I5_PRIVATE_MODULE_READONLY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
