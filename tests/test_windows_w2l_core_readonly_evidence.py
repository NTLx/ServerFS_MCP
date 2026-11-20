"""Source-only contracts for W2L existing-Core failure diagnosis.

These tests do not access Task Scheduler, files under LocalAppData,
Windows event logs, credentials or subprocesses.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _ROOT / "deployment/windows/v0250-w2l-core-readonly-state.py"
_HOST = _ROOT / "deployment/windows/v0250-w2l-core-failure-evidence.ps1"


def test_python_probe_never_touches_core_lifecycle_or_exposes_raw_fields() -> None:
    source = _PROBE.read_text(encoding="utf-8")
    ast.parse(source, filename=str(_PROBE))
    assert "snap = core.status()" in source
    assert "state = core._read_state()" in source
    assert "process_identity(os.getpid())" in source
    assert 'snap.get("enabled") is False' in source
    assert 'snap.get("core_alive") is False' in source
    assert "core.start(" not in source
    assert "core.stop(" not in source
    assert "core.pause(" not in source
    assert "core.restart(" not in source
    assert '"core_pid":' not in source
    assert '"core_image_path":' not in source


def test_hyphenated_core_stage_values_are_checked_by_exact_allowlist() -> None:
    script = _HOST.read_text(encoding="utf-8")
    probe = _PROBE.read_text(encoding="utf-8")
    # Original W2L host failed after core_listener_owner: this generic
    # regex rejected the valid hyphenated state emitted by the probe.
    assert "if ($name -eq 'core_last_state_stage')" in script
    assert "$value -cnotin @('disabled-never-started','owner-started','UNVERIFIED')" in script
    assert "elseif ($value -notmatch '^[A-Za-z0-9_]+$')" in script
    for stage in ("disabled-never-started", "owner-started"):
        assert f"'{stage}'" in script
        assert f'"{stage}"' in probe


def test_host_probe_is_read_only_and_event_data_are_allowlisted() -> None:
    script = _HOST.read_text(encoding="utf-8")
    assert "Get-ScheduledTaskInfo" in script
    assert "Get-WinEvent" in script
    assert "ServerFS MCP Core" in script
    assert "'2026-11-12T00:20:00Z'" in script
    assert "'2026-11-12T00:43:00Z'" in script
    assert "'TaskName', 'Task', 'TaskPath'" in script
    assert "'ResultCode','ErrorCode','ReturnCode','FailureCode'" in script
    assert "Get-ScheduledTask -ErrorAction Stop" in script
    for forbidden in (
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Disable-ScheduledTask",
        "Enable-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "New-ScheduledTask",
        "Set-ScheduledTask",
        "Start-Process",
        "Invoke-WebRequest",
    ):
        assert forbidden not in script
