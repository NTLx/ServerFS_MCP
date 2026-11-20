"""Pure, no-environment-mutation checks for W2g installed-import gate routing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def gate():
    source = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "windows"
        / "v0250-w2g-installed-import-gate.py"
    )
    spec = importlib.util.spec_from_file_location("w2g_import_gate", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unknown_component_is_rejected_before_import(gate) -> None:
    assert gate.main(["legacy-tunnel"]) == 2
    assert gate.main([]) == 2


def test_owner_import_routing_requires_native_and_no_bridge(gate, monkeypatch) -> None:
    imported: list[str] = []
    monkeypatch.setattr(gate, "_private_environment", lambda _label: Path("C:/mock/site"))
    monkeypatch.setattr(gate.importlib.metadata, "version", lambda _name: "0.24.2")
    monkeypatch.setattr(gate.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(gate, "_module_from_site", lambda name, _site: imported.append(name))
    gate.verify("bridge-owner-env")
    assert "serverfs_mcp.windows_bridge_service" in imported
    assert "serverfs_mcp.windows_core_service" in imported
    assert "serverfs_windows_native.serverfs_windows_native" in imported
    assert "serverfs_mcp.windows_component_cli" in imported
    assert "serverfs_agent_bridge.main" not in imported


def test_bridge_runtime_import_routing_excludes_product(gate, monkeypatch) -> None:
    imported: list[str] = []
    monkeypatch.setattr(gate, "_private_environment", lambda _label: Path("C:/mock/site"))
    monkeypatch.setattr(gate.importlib.metadata, "version", lambda _name: "0.24.2")
    monkeypatch.setattr(gate.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(gate, "_module_from_site", lambda name, _site: imported.append(name))
    gate.verify("bridge-runtime-env")
    assert "serverfs_agent_bridge.main" in imported
    assert "claude_agent_sdk" in imported
    assert "acp" in imported
    assert "websockets" in imported
    assert "serverfs_mcp" not in imported


def test_mismatched_wheel_version_fails_closed(gate, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_private_environment", lambda _label: Path("C:/mock/site"))
    monkeypatch.setattr(gate.importlib.metadata, "version", lambda _name: "0.23.0")
    with pytest.raises(gate.ImportGateError, match="version"):
        gate.verify("core-owner-env")
