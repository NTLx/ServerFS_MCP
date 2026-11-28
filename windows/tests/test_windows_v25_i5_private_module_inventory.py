"""I5 installed private owner package inventory: nonmutating contracts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-private-module-inventory.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_module_inventory", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_i5_module_inventory_fails_closed_on_bad_environment() -> None:
    module = _module()
    module.require(True, "good")
    with pytest.raises(module.I5PrivateInventoryError):
        module.require(False, "unverified")


def test_i5_module_inventory_is_read_only_and_does_not_echo_paths() -> None:
    text = _SOURCE.read_text(encoding="utf-8")
    for required in (
        "importlib.util.find_spec(",
        "serverfs_mcp.windows_v25.legacy_bridge_ownership",
        "package.is_relative_to(root)",
        "sys.flags.isolated",
        "IsUserAnAdmin()",
        "I5_PRIVATE_MODULE_PACKAGE=INSTALLED",
        "I5_PRIVATE_MODULE_READONLY=PASS",
    ):
        assert required in text
    for denied in (
        "Scheduler().",
        "Get-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "subprocess.",
        "os.system(",
        ".write_text(",
        ".write_bytes(",
        "print(sys.executable)",
        "print(package)",
        "pip install",
        "git push",
        "secret_hex",
    ):
        assert denied not in text
