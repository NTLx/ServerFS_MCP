"""Contract tests for real HTTPS I5 two-Core/shared-Bridge nonproduction canary."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-two-core-public-bridge-smoke.py"
)
spec = importlib.util.spec_from_file_location("i5_public_dual_bridge_smoke_contract", SOURCE)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_real_public_two_Core_and_separate_connector_identity() -> None:
    assert smoke.BRIDGE == "i5-shared"
    assert smoke.ROUTES == (
        ("i5-core-a", "i5-edge-a", "workpc-v25a.ntlx.top", 18873),
        ("i5-core-b", "i5-edge-b", "workpc-v25b.ntlx.top", 18874),
    )
    assert {r[0] for r in smoke.ROUTES} == {"i5-core-a", "i5-core-b"}
    assert {r[1] for r in smoke.ROUTES} == {"i5-edge-a", "i5-edge-b"}


@pytest.mark.parametrize(
    "status,valid",
    [
        ({"registered": True, "state": "Disabled", "enabled": False, "running": False}, True),
        ({"registered": True, "state": "Ready", "enabled": True, "running": False}, False),
        ({"registered": True, "state": "Disabled", "enabled": False, "running": True}, False),
        ({"registered": False, "state": "Absent", "enabled": False, "running": False}, False),
    ],
)
def test_only_exact_disabled_offline_owner_is_clean(status: dict, valid: bool) -> None:
    assert smoke.is_disabled(status) is valid


def test_public_mcp_result_must_be_nonerror_structured_mapping() -> None:
    assert smoke.data(
        SimpleNamespace(is_error=False, structured_content={"result": {"ok": True}})
    ) == {"ok": True}
    with pytest.raises(smoke.I5PublicBridgeRefusal):
        smoke.data(SimpleNamespace(is_error=True, structured_content={"result": {"ok": True}}))
    with pytest.raises(smoke.I5PublicBridgeRefusal):
        smoke.data(SimpleNamespace(is_error=False, structured_content={"result": "invalid"}))


def test_live_i5_safe_i7_owner_check_ignores_live_i5_scheduler_rows(monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import readiness, storage, tasks

    checked = []

    def load(component, instance, connector):
        checked.append(connector)
        assert component == "cloudflare" and instance == "i7-public"
        return SimpleNamespace(component=component, instance_id=instance, connector_id=connector)

    class FakeScheduler:
        def status(self, _manifest):
            return {
                "registered": True,
                "state": "Disabled",
                "enabled": False,
                "running": False,
                "readiness": "offline",
            }

    monkeypatch.setattr(storage, "load", load)
    monkeypatch.setattr(tasks, "Scheduler", FakeScheduler)
    monkeypatch.setattr(readiness, "_current_identity", lambda _manifest: None)
    smoke.original_i7_owners_disabled()
    assert checked == ["i7-edge-a", "i7-edge-b"]


def test_live_i5_safe_i7_owner_check_refuses_one_running_original(monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import readiness, storage, tasks

    def load(component, instance, connector):
        return SimpleNamespace(component=component, instance_id=instance, connector_id=connector)

    class FakeScheduler:
        def status(self, manifest):
            running = manifest.connector_id == "i7-edge-b"
            return {
                "registered": True,
                "state": "Running" if running else "Disabled",
                "enabled": running,
                "running": running,
                "readiness": "owner_alive" if running else "offline",
            }

    monkeypatch.setattr(storage, "load", load)
    monkeypatch.setattr(tasks, "Scheduler", FakeScheduler)
    monkeypatch.setattr(readiness, "_current_identity", lambda _manifest: None)
    with pytest.raises(smoke.I5PublicBridgeRefusal):
        smoke.original_i7_owners_disabled()


def test_host_security_cross_core_denial_and_no_provider_submit() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for snippet in (
        "missing.status_code == wrong.status_code == cross.status_code == 401",
        '"Host": "untrusted.invalid"',
        '"Origin": "https://untrusted.invalid"',
        '"list_agent_runtimes"',
        '"submit_agent_task"',
        'session.call_tool("list_workdirs", {})',
        'session.call_tool("list_agent_runtimes", {})',
        'fixture.get("repo_ops") is False',
        'connector_cli(core, connector, "start")',
        "original_i7_owners_disabled()",
        'state.get("readiness") == "offline"',
        "_current_identity(manifest) is None",
        '["--run-fixed-once"]',
        "stage.postflight()",
        "smoke.signed_dual_core(bindings, sid)",
        "I5_PUBLIC_B_SURVIVES_CONNECTOR_A_STOP=PASS",
        "I5_PUBLIC_B_SURVIVES_CORE_A_STOP=PASS",
        "I5_PUBLIC_FIVE_SCOPED_TASKS_DISABLED=",
        "I5_PUBLIC_TWO_CORE_SHARED_BRIDGE_REAL_HTTPS_E2E=PASS",
    ):
        assert snippet in source
    for forbidden in (
        'session.call_tool("submit_agent_task"',
        'session.call_tool("run_repo_action"',
        'session.call_tool("answer_agent_question"',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "i7.postflight()",
        "cloudflare.request(",
        "ANTHROPIC_API_KEY",
        "http.env\nprint(",
        "tunnel.token\nprint(",
        "git push",
    ):
        assert forbidden not in source
