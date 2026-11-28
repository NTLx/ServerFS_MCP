"""Synthetic Core-only acceptance script gates, no real host/service actions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _smoke():
    source = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2i-core-smoke.py"
    spec = importlib.util.spec_from_file_location("w2i_core_smoke", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_port_and_other_components_are_never_lifecycle_mutated() -> None:
    script = _smoke()
    assert script._PORT == 18766
    assert script._PORT != 18765
    assert script._HOSTNAME == "workpc-serverfs.ntlx.top"
    assert script._OTHER == ("bridge", "cloudflare", "openai")


def test_check_core_requires_owned_listener_and_valid_task() -> None:
    script = _smoke()
    good = {
        "registered": True,
        "task_valid": True,
        "enabled": True,
        "core_alive": True,
        "listener_owner": "verified",
    }
    script._check_core(good, running=True)
    with pytest.raises(script.W2iError):
        script._check_core({**good, "listener_owner": "not_verified"}, running=True)
    script._check_core({**good, "enabled": False, "core_alive": False}, running=False)


def test_unauthenticated_get_requires_401_and_no_credentials(monkeypatch) -> None:
    script = _smoke()
    seen = []

    class FakeResponse:
        status = 401

    class FakeConnection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 18766, 8)

        def request(self, method, path, headers):
            seen.append((method, path, headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(script.http.client, "HTTPConnection", FakeConnection)
    assert script._http_unauthenticated("127.0.0.1:18766") == 401
    assert len(seen) == 1
    assert seen[0][0:2] == ("GET", "/mcp")
    assert seen[0][2]["Host"] == "127.0.0.1:18766"
    assert "Authorization" not in seen[0][2]


def test_owned_core_stop_is_attempted_on_http_gate_failure(monkeypatch) -> None:
    script = _smoke()
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(
        native_lifecycle, "window_local_app_data", lambda: Path("C:/synthetic"), raising=False
    )
    calls = []

    def fake_invoke(_root, component, action):
        calls.append((component, action))
        if action == "status" and component == "core":
            return {
                "registered": True,
                "task_valid": True,
                "enabled": False,
                "core_alive": False,
            }
        if component == "core" and action == "start":
            return {
                "registered": True,
                "task_valid": True,
                "enabled": True,
                "core_alive": True,
                "listener_owner": "verified",
            }
        if component == "core" and action == "stop":
            return {
                "registered": True,
                "task_valid": True,
                "enabled": False,
                "core_alive": False,
            }
        return {"registered": True, "task_valid": True, "enabled": False}

    monkeypatch.setattr(script, "_invoke_component", fake_invoke)

    def reject_http(_host):
        raise script.W2iError("synthetic auth refusal")

    monkeypatch.setattr(script, "_http_unauthenticated", reject_http)
    with pytest.raises(script.W2iError, match="synthetic"):
        script._stage([])
    assert ("core", "start") in calls
    assert ("core", "stop") in calls
    assert all(action not in {"start", "stop"} for comp, action in calls if comp != "core")


def test_safe_cli_failure_reason_has_no_raw_private_text() -> None:
    script = _smoke()
    assert (
        script._classify_cli_failure(
            "serverfs-windows-component: Core activation failed; rollback incomplete"
        )
        == "core-start-rollback-incomplete"
    )
    assert (
        script._classify_cli_failure("Cannot access C:/Users/private/.config/secret-token")
        == "unclassified-redacted-cli-error"
    )


def test_both_start_and_cleanup_failures_are_recorded(monkeypatch) -> None:
    script = _smoke()
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(
        native_lifecycle, "window_local_app_data", lambda: Path("C:/synthetic"), raising=False
    )
    calls = []

    def fake_invoke(_root, component, action):
        calls.append((component, action))
        if component == "core" and action == "start":
            raise script.W2iError("core-start: exit=2; reason=core-task-contract-drift")
        if component == "core" and action == "stop":
            raise script.W2iError("core-stop: exit=2; reason=core-disable-refused")
        if component == "core":
            return {
                "registered": True,
                "task_valid": True,
                "enabled": False,
                "core_alive": False,
            }
        return {"registered": True, "task_valid": True, "enabled": False}

    monkeypatch.setattr(script, "_invoke_component", fake_invoke)
    events = []
    with pytest.raises(script.W2iError, match="core-stop"):
        script._stage(events)
    assert ("core", "start") in calls
    assert ("core", "stop") in calls
    assert any("Core start/HTTP stage: FAILED (core-start: exit=2" in event for event in events)
    assert any("Core owned stop: FAILED (core-stop: exit=2" in event for event in events)


def test_outer_powershell_has_exact_core_only_disable_failsafe() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2i-core-smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "if ($coreActivationAttempted)" in script
    assert "Disable-ScheduledTask -TaskName $core -TaskPath" in script
    assert "Stop-ScheduledTask" not in script
    assert "Core fail-safe: FAILED" in script


def test_w2n_retest_requires_installed_wheel_byte_base_and_generation_proofs() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deployment/windows/v0250-w2i-core-smoke.ps1"
    ).read_text(encoding="utf-8")
    assert "v0250-w2g-verify-installed.py" in script
    assert "v0250-w2j-core-generation-proof.py" in script
    assert "v0250-w2n-core-venv-home-gate.py" in script
    assert "919ec7023a8920b6ef488653d7f17b7e63896c752b41e5b00565c7b6322112b1" in script
    assert "127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3" in script
    assert "w2n-pythonw-20261112T0402231886660Z" in script
    assert "3102218157180fa7a70204156bbe9b85ba3b5b7a1d82cc40d214484ae04e007a" not in script
    assert script.index("$stage = 'W2n-exact-Core-installed-Wheel-proof'") < script.index(
        "$stage = 'isolated-Core-only-start-probe-stop'"
    )
    assert script.index("$baseProof 2>&1 | Out-Null") < script.index(
        "$stage = 'source-regressions'"
    )
    assert "$_.TaskPath -eq '\\'" in script
