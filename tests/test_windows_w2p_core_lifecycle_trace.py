"""Non-mutating synthetic checks of Core activation/rollback stage tracing.

No real scheduler, service, agent, process, private wheel or network action.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from serverfs_mcp import windows_core_control as control


def _trace_module():
    path = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2p-core-lifecycle-trace.py"
    )
    spec = importlib.util.spec_from_file_location("w2p_core_trace", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_known_task_operations_have_only_fixed_labels() -> None:
    tracer = _trace_module()
    assert tracer._operation(("/Query", "/TN", "ServerFS MCP Core", "/XML")) == "query"
    assert tracer._operation(("/Change", "/TN", "ServerFS MCP Core", "/ENABLE")) == "change-enable"
    assert (
        tracer._operation(("/Change", "/TN", "ServerFS MCP Core", "/DISABLE")) == "change-disable"
    )
    assert tracer._operation(("/Run", "/TN", "ServerFS MCP Core")) == "run"
    assert tracer._operation(("/End", "/TN", "ServerFS MCP Core")) == "end"
    assert tracer._operation(("/Create", "/TN", "ServerFS MCP Core")) == "unrecognized"


def test_private_exception_values_are_never_returned() -> None:
    tracer = _trace_module()
    assert (
        tracer._category(control.CoreControlError("Core registered task identity has drifted"))
        == "Core registered task identity has drifted"
    )
    assert (
        tracer._category(control.CoreControlError("private C:/Users/someone/.env"))
        == "CoreControlError:other"
    )
    assert tracer._category(ValueError("sensitive config")) == "other:ValueError"


@pytest.mark.parametrize("action", ["start", "stop"])
def test_original_methods_restored_and_rollback_refusal_traced(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    tracer = _trace_module()
    monkeypatch.setattr(tracer, "_require_dedicated_runtime", lambda: None)

    def initial_call(*_args):
        return subprocess.CompletedProcess([], 0)

    def initial_owner():
        return object()

    def initial_wait(**_kwargs):
        return {}

    def initial_status():
        return {}

    monkeypatch.setattr(control, "_task_call", initial_call)
    monkeypatch.setattr(control, "_require_owned", initial_owner)
    monkeypatch.setattr(control, "_wait_for_core", initial_wait)
    monkeypatch.setattr(control, "status", initial_status)

    def failed_stage():
        control._task_call("/Change", "/TN", "ServerFS MCP Core", "/ENABLE")
        try:
            control._require_owned()
        except control.CoreControlError:
            pass
        try:
            control._wait_for_core(alive=True)
        except control.CoreControlError:
            pass
        control._require_owned()

    counter = {"count": 0}

    def refuse_owner():
        counter["count"] += 1
        raise control.CoreControlError("Core registered task identity has drifted")

    def refuse_wait(*, alive: bool, timeout_seconds: float = 20.0):
        raise control.CoreControlError("Core lifecycle proof timed out")

    monkeypatch.setattr(control, "_require_owned", refuse_owner)
    monkeypatch.setattr(control, "_wait_for_core", refuse_wait)
    monkeypatch.setattr(control, "start", failed_stage)
    monkeypatch.setattr(control, "stop", failed_stage)
    before = {
        "_task_call": control._task_call,
        "_require_owned": control._require_owned,
        "_wait_for_core": control._wait_for_core,
        "status": control.status,
    }
    result = tracer._run_once(action)
    assert result["ok"] is False
    assert result["reason"] == "Core registered task identity has drifted"
    assert "taskcall:change-enable:rc=0" in result["trace"]
    assert "owner:refused:Core registered task identity has drifted" in result["trace"]
    assert "wait:refused:Core lifecycle proof timed out" in result["trace"]
    assert result["trace"][-1].startswith(action + ":failed:")
    assert counter["count"] == 2
    for name, original in before.items():
        assert getattr(control, name) is original


def test_48_event_budget_keeps_late_rollback_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = _trace_module()
    monkeypatch.setattr(tracer, "_require_dedicated_runtime", lambda: None)
    monkeypatch.setattr(control, "_task_call", lambda *_args: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(control, "_require_owned", lambda: None)
    monkeypatch.setattr(control, "_wait_for_core", lambda **_kwargs: None)
    monkeypatch.setattr(control, "status", lambda: {})

    def delayed_failure():
        for _ in range(90):
            control._task_call("/Query", "/TN", "ServerFS MCP Core", "/XML")
        raise control.CoreControlError("Core activation failed; rollback incomplete")

    monkeypatch.setattr(control, "start", delayed_failure)
    result = tracer._run_once("start")
    assert len(result["trace"]) <= 48
    assert result["trace"][0] == "start:entered"
    assert result["trace"][-1] == "start:failed:Core activation failed; rollback incomplete"


def test_w2i_source_uses_redacted_core_tracer_only_for_start_stop() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2i-core-smoke.py"
    ).read_text(encoding="utf-8")
    assert 'traced = component == "core" and action in ("start", "stop")' in source
    assert "Core tracer did not return a verified redacted report" in source
    assert '"trace": []' in (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2p-core-lifecycle-trace.py"
    ).read_text(encoding="utf-8")


def test_parent_accepts_only_safe_tracer_labels_and_never_raw_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    from test_windows_w2i_core_smoke_contract import _smoke

    smoke = _smoke()
    runtime = tmp_path / "core-owner-env"
    exe = runtime / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic")
    safe = {
        "ok": False,
        "trace": [
            "start:entered",
            "taskcall:change-enable:rc=0",
            "owner:refused:Core registered task identity has drifted",
        ],
        "reason": "Core activation failed; rollback incomplete",
    }
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout=json.dumps(safe), stderr="private C:/Users/foo/.env"
        ),
    )
    with pytest.raises(smoke.W2iError) as caught:
        smoke._invoke_component(runtime, "core", "start")
    assert "owner:refused:Core registered task identity has drifted" in str(caught.value)
    assert "C:/Users" not in str(caught.value)

    safe["trace"].append("private C:/Users/foo/.env")
    with pytest.raises(smoke.W2iError, match="verified redacted report") as blocked:
        smoke._invoke_component(runtime, "core", "start")
    assert "C:/Users" not in str(blocked.value)
