"""I2 read-only registration preconditions: source-only regression checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i2-registration-gate-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i2_registration_readonly", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i2_gate_requires_exact_nondefault_bridge_and_fresh_targets() -> None:
    script = _SOURCE.read_text(encoding="utf-8")
    assert 'INSTANCE = "i2-smoke"' in script
    assert '"component": "bridge"' in script
    assert '"schema": 2' in script
    assert "not manifest_path.exists()" in script
    assert "not task_xml.exists()" in script
    assert "load_native_bridge_plan(manifest)" in script
    assert "_assert_instance_isolation(manifest)" in script


def test_i2_gate_never_mutates_installed_state_or_logs_private_documents() -> None:
    script = _SOURCE.read_text(encoding="utf-8")
    for forbidden in (
        "storage.stage(",
        "storage.prepare(",
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "secret_hex",
        "read_text(",
        "read_bytes(",
        "open(",
        "git push",
        "Stop-Process",
        "Set-Acl",
    ):
        assert forbidden not in script
    assert "I2_GATE_ISOLATION=PASS" in script
    assert "I2_GATE_STORAGE_PRECONDITIONS=PASS" in script


def test_i2_gate_denial_is_typed_and_nonsecret() -> None:
    module = _module()
    with pytest.raises(module.I2GateError):
        module.require(False)
    assert module.require(True) is None
