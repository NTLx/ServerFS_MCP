"""Pure Windows I2 Bridge lifecycle smoke contracts; never execute __main__."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i2-live-smoke.py"


def _module():
    spec = importlib.util.spec_from_file_location("serverfs_i2_smoke_source", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate():
    from collections import OrderedDict

    production = OrderedDict(
        [
            ("ServerFS Native Tunnel", (True, "Running")),
            ("ServerFS v25 Core", (True, "Running")),
            ("ServerFS v25 Cloudflare Connector", (True, "Running")),
            ("ServerFS v25 Agent Bridge", (False, None)),
            ("ServerFS v25 OpenAI Connector", (False, None)),
        ]
    )
    rows = [
        {"name": name, "state": state or "Disabled", "enabled": enabled}
        for name, (enabled, state) in production.items()
    ]
    rows.append(
        {"name": "ServerFS v25 i2-smoke Agent Bridge", "state": "Disabled", "enabled": False}
    )
    return SimpleNamespace(
        PRODUCTION=production,
        _scheduler_inventory=lambda: rows,
        verify_scheduler_rows=lambda subset: len(subset) == 5 or 1 / 0,
    ), rows


def test_smoke_is_exact_single_fixture_only() -> None:
    smoke = _module()
    assert smoke.INSTANCE == "i2-smoke"
    assert smoke.TASK == "ServerFS v25 i2-smoke Agent Bridge"
    source = _SCRIPT.read_text(encoding="utf-8")
    assert 'cli("start")' in source
    assert 'cli("stop")' in source
    assert '"--instance",' in source
    assert "I2_SIGNED_PIPE_AND_KERNEL_JOB=PASS" in source
    assert "I2_UNAUTHORIZED_HMAC=DENIED" in source
    assert "I2_FIXTURE_STOP=" in source
    assert "I2_PRODUCTION_TASKS_AND_CORE=UNCHANGED" in source
    assert "Scheduler().install(" not in source
    assert "Stop-Process" not in source
    assert "Stop-ScheduledTask" not in source
    assert "remove-item" not in source.lower()
    assert "git push" not in source.lower()


@pytest.mark.parametrize("action", ["restart", "install", "uninstall", "prepare", "configure"])
def test_i2_smoke_refuses_other_lifecycle_verbs(action: str) -> None:
    smoke = _module()
    with pytest.raises(smoke.I2SmokeError, match="not allowlisted"):
        smoke.cli(action)


def test_fixture_state_requires_one_exact_disabled_task() -> None:
    smoke = _module()
    gate, rows = _gate()
    assert smoke.fixture_state(gate) == ("Disabled", False)
    rows[-1]["state"] = "Running"
    assert smoke.fixture_state(gate) == ("Running", False)
    rows.append(dict(rows[-1]))
    with pytest.raises(smoke.I2SmokeError, match="ambiguous"):
        smoke.fixture_state(gate)


def test_production_snapshot_proves_five_existing_components_only() -> None:
    smoke = _module()
    gate, rows = _gate()
    baseline = smoke.production_snapshot(gate)
    assert len(baseline) == 5
    assert all(row[0] != smoke.TASK for row in baseline)
    rows[0]["state"] = "Disabled"
    assert smoke.production_snapshot(gate) != baseline


def test_cli_failures_redact_subprocess_stderr(monkeypatch, capsys) -> None:
    smoke = _module()
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, b"", b"PRIVATE-KEY-SECRET"),
    )
    with pytest.raises(smoke.I2SmokeError) as caught:
        smoke.cli("start")
    assert "PRIVATE-KEY-SECRET" not in str(caught.value)
    assert "PRIVATE-KEY-SECRET" not in capsys.readouterr().err


def test_smoke_requires_hmac_and_real_job_generation_not_v1_fallback() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "make_bridge_peer_validator" in source
    assert "CoreGrantBridgeClient" in source
    assert "server_pid_validator" in source
    assert "bad HMAC" in source
    assert "AgentBridgeRemoteError" in source
    assert '"CORE_NOT_AUTHORIZED"' in source
    assert "AgentBridgeClient(" not in source
    assert "secrets.token_bytes(" not in source
    assert "load_native_bridge_plan(manifest)" in source
