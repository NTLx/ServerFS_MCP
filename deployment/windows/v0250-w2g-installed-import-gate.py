"""W2g isolated installed-wheel imports; never run services or mutate packages.

Execute using the exact installed private environment interpreter:
    python.exe -I -B v0250-w2g-installed-import-gate.py bridge-owner-env

This file-based probe avoids Windows PowerShell 5.1 native-argv quote removal
affecting `python -c '... "..." ...'`. It reports no paths, secrets, or
tracebacks and never imports an SDK into a Product-only owner environment.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path

_EXPECTED = {
    "bridge-owner-env": "owner",
    "core-owner-env": "owner",
    "cloudflare-connector-env": "owner",
    "openai-connector-env": "owner",
    "bridge-runtime-env": "bridge",
}
_VERSION = "0.24.2"
_PYTHON_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class ImportGateError(Exception):
    """Sanitized imported-package isolation refusal."""


def _module_from_site(module_name: str, site: Path) -> None:
    module = importlib.import_module(module_name)
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str):
        raise ImportGateError("module origin is unavailable")
    if not Path(origin).resolve(strict=True).is_relative_to(site):
        raise ImportGateError("import did not resolve inside the dedicated environment")


def _private_environment(label: str) -> Path:
    if label not in _EXPECTED:
        raise ImportGateError("component identity is not fixed")
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ImportGateError("isolated no-bytecode Python invocation is required")
    if sys.platform != "win32":
        raise ImportGateError("Windows is required")
    prefix = Path(sys.prefix).resolve(strict=True)
    executable = Path(sys.executable).resolve(strict=True)
    if prefix.name.casefold() != label.casefold():
        raise ImportGateError("active environment prefix identity is invalid")
    if executable != (prefix / "Scripts" / "python.exe").resolve(strict=True):
        raise ImportGateError("active Python interpreter differs from expected venv")
    site = prefix / "Lib" / "site-packages"
    if not site.is_dir():
        raise ImportGateError("active environment site-packages is unavailable")
    return site


def verify(label: str) -> None:
    site = _private_environment(label)
    if _EXPECTED[label] == "owner":
        if importlib.metadata.version("serverfs-mcp") != _VERSION:
            raise ImportGateError("Product Wheel version does not match accepted candidate")
        if importlib.metadata.version("serverfs-windows-native") != _VERSION:
            raise ImportGateError("Native Wheel version does not match accepted candidate")
        if importlib.util.find_spec("serverfs_agent_bridge") is not None:
            raise ImportGateError("foreign Agent Bridge package is present in owner")
        for module in (
            "serverfs_mcp",
            "serverfs_windows_native",
            "serverfs_windows_native.serverfs_windows_native",
            "serverfs_mcp.windows_core_service",
            "serverfs_mcp.windows_bridge_service",
            "serverfs_mcp.windows_connector_service",
            "serverfs_mcp.windows_component_cli",
        ):
            _module_from_site(module, site)
    else:
        if importlib.metadata.version("serverfs-agent-bridge") != _VERSION:
            raise ImportGateError("Bridge Wheel version does not match accepted candidate")
        if importlib.util.find_spec("serverfs_mcp") is not None:
            raise ImportGateError("foreign Product package is present in Bridge runtime")
        for module in (
            "serverfs_agent_bridge.main",
            "serverfs_agent_bridge.service",
            "serverfs_agent_bridge.windows_ipc",
            "serverfs_agent_bridge.windows_pipe",
            "claude_agent_sdk",
            "acp",
            "websockets",
        ):
            _module_from_site(module, site)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in _EXPECTED:
        print("W2G_IMPORT_GATE=FAILED (component identity)", file=sys.stderr)
        return 2
    try:
        verify(argv[0])
    except Exception as exc:  # noqa: BLE001 - avoid paths/env/token in output
        error = type(exc).__name__
        missing = getattr(exc, "name", None)
        if isinstance(exc, ModuleNotFoundError) and isinstance(missing, str):
            if _PYTHON_MODULE.fullmatch(missing):
                error += f", missing={missing}"
        print(f"W2G_IMPORT_GATE=FAILED ({error})", file=sys.stderr)
        return 2
    print("W2G_IMPORT_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
