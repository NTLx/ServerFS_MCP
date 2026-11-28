"""I6 named Cloudflare Connector Disabled-only acceptance gate contracts.

All fixtures intercept subprocess/Task Scheduler. Tests do not stage venvs,
create installed services or read real provider credentials.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i6-named-connectors-stage.py"
)
_SPEC = importlib.util.spec_from_file_location("i6_named_stage_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
stage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(stage)


def test_i6_fixed_two_connector_names_and_no_implicit_run() -> None:
    assert stage.INSTANCE == "i4-a"
    assert stage.IDS == ("i6-edge-a", "i6-edge-b")
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "def preflight()" in source and "def stage()" in source
    assert "scheduler.install(manifest)" in source
    assert "fixture_tasks(installed=True)" in source
    assert "I6_NATIVE_DISABLED_ONLY_STAGE=PASS" in source
    assert "scheduler.start(" not in source
    assert "Scheduler().start(" not in source
    assert "cloudflared tunnel" not in source
    assert "DISABLED_I6_NON_EXECUTABLE_FIXTURE" in source


def test_i6_candidate_scheduler_probe_accepts_absent_and_exact_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {"data": b"[]"}

    def subprocess_stub(argv, **kwargs):
        assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert "Get-ScheduledTask" in argv[-1]
        assert "/Run" not in argv[-1] and "/Delete" not in argv[-1]
        return subprocess.CompletedProcess(argv, 0, output["data"])

    monkeypatch.setattr(stage.subprocess, "run", subprocess_stub)
    stage.fixture_tasks(installed=False)

    rows = [
        {
            "name": f"ServerFS v25 {stage.INSTANCE} Cloudflare Connector {connector_id}",
            "state": "Disabled",
            "enabled": False,
        }
        for connector_id in stage.IDS
    ]
    output["data"] = json.dumps(rows).encode()
    stage.fixture_tasks(installed=True)
    with pytest.raises(stage.I6StageError, match="occupied"):
        stage.fixture_tasks(installed=False)

    rows[0]["enabled"] = True
    output["data"] = json.dumps(rows).encode()
    with pytest.raises(stage.I6StageError, match="Disabled"):
        stage.fixture_tasks(installed=True)


def test_i6_candidate_scheduler_probe_fails_closed_on_ambiguous_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = {"data": b"{}", "exit": 0}

    def subprocess_stub(argv, **kwargs):
        return subprocess.CompletedProcess(argv, wrong["exit"], wrong["data"])

    monkeypatch.setattr(stage.subprocess, "run", subprocess_stub)
    for data in (b"{}", b"not-json", b"[{},{}]"):
        wrong["data"] = data
        with pytest.raises(stage.I6StageError):
            stage.fixture_tasks(installed=True)
    wrong["exit"] = 7
    with pytest.raises(stage.I6StageError):
        stage.fixture_tasks(installed=False)


def test_i6_preflight_refuses_existing_venv_or_service_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    root = tmp_path / "instance"
    root.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(
        stage,
        "helper",
        lambda filename, name: SimpleNamespace(main=lambda: calls.append("I4_postflight")),
    )
    monkeypatch.setattr(
        stage,
        "fixture_tasks",
        lambda *, installed: calls.append("I6_absence" if not installed else "I6_present"),
    )
    monkeypatch.setattr(storage, "root", lambda instance: root)
    monkeypatch.setattr(storage, "check_private", lambda path, *, directory: None)
    # Simulate only this script's platform gate without changing process-global sys/os.
    monkeypatch.setattr(
        stage,
        "sys",
        SimpleNamespace(
            platform="win32", flags=SimpleNamespace(isolated=1), dont_write_bytecode=True
        ),
    )
    monkeypatch.setattr(stage, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        stage.ctypes,
        "windll",
        SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False)),
        raising=False,
    )
    stage.preflight()
    assert calls == ["I4_postflight", "I6_absence"]

    for relative in ("services/cloudflare", "envs/cloudflare", "build/cloudflare-owner"):
        probe = root / relative
        probe.mkdir(parents=True)
        with pytest.raises(stage.I6StageError, match="occupied"):
            stage.preflight()
        probe.rmdir()
