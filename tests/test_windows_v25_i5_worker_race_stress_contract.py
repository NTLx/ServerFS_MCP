"""Static safety contract for the installed-Worker-only SQLite churn gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i5-worker-race-native-stress.py"
)
_SPEC = importlib.util.spec_from_file_location("i5_native_sqlite_stress_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
stress = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(stress)


def test_installed_worker_stress_has_bounded_isolated_temp_scope() -> None:
    assert stress.WORKERS == 4
    assert stress.ROUNDS == 75
    source = _SCRIPT.read_text(encoding="utf-8")
    for token in (
        "TemporaryDirectory(",
        "ThreadPoolExecutor(max_workers=WORKERS)",
        "Path(installed_store.__file__).resolve(strict=True)",
        "private_state.verify_private_file(",
        "I5_INSTALLED_WAL_SHM_CHURN=PASS",
        "Get-ScheduledTask",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
    ):
        assert token in source
    for forbidden in (
        "Set-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Scheduler().start",
        "Scheduler().stop",
        "subprocess.Popen(",
        "uv pip install",
        "cloudflared tunnel",
        "task.submit",
    ):
        assert forbidden not in source


def test_installed_worker_stress_guard_fails_closed() -> None:
    with pytest.raises(stress.I5WorkerStressError):
        stress.require(False, "unsafe")
    stress.require(True, "accepted")
