"""Source-only safety contract for the installed I4 owned-pair live smoke.

No Scheduler actions, host queries, process starts, network or credentials.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_windows_v25_i4_preflight import _module as preflight_module
from test_windows_v25_i4_preflight import _rows as preflight_rows

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i4-independent-live-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i4_live_smoke_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def _generation(pid: int, created: int):
    return SimpleNamespace(pid=pid, creation_time=created)


def test_i4_live_tasks_are_only_two_exact_owned_pairs() -> None:
    m = _module()
    assert m.IDS == ("i4-a", "i4-b")
    assert m.PORTS == {"i4-a": 18871, "i4-b": 18872}
    assert m.TASKS == (
        ("bridge", "i4-a"),
        ("bridge", "i4-b"),
        ("core", "i4-a"),
        ("core", "i4-b"),
    )


def test_i4_process_generation_requires_same_pid_and_creation_time() -> None:
    m = _module()
    a, b, reused = _generation(12, 100), _generation(13, 200), _generation(12, 200)
    assert m.same_generation(a, _generation(12, 100))
    assert not m.same_generation(a, b)
    assert not m.same_generation(a, reused)
    assert m.new_generation(a, b)
    assert m.new_generation(a, reused)
    assert not m.new_generation(a, _generation(12, 100))
    assert not m.same_generation(None, a)
    assert not m.new_generation(None, a)


def test_i4_protected_snapshot_excludes_fixture_tasks_but_revalidates_all() -> None:
    m = _module()
    gate = preflight_module()
    rows = preflight_rows(gate, candidates=True)
    called = []

    def scheduler():
        called.append("read")
        return rows

    gate.scheduler_rows = scheduler
    result = m.protected_baseline(gate)
    assert called == ["read"]
    assert len(result) == 10
    assert not any("i4-a" in name or "i4-b" in name for name, _state, _enabled in result)


@pytest.mark.parametrize(
    ("component", "instance", "action"),
    [
        ("core", "default", "stop"),
        ("bridge", "i2-smoke", "start"),
        ("cloudflare", "i4-a", "stop"),
        ("core", "i4-a", "remove"),
        ("core", "i4-b", "uninstall"),
    ],
)
def test_i4_cli_rejects_foreign_owner_or_unsafe_actions_before_spawning(
    component: str, instance: str, action: str
) -> None:
    m = _module()
    with pytest.raises(m.I4SmokeError):
        m.cli(component, instance, action)


def test_i4_wrong_bearer_headers_never_reuse_the_live_core_token() -> None:
    m = _module()
    live = "live-core-token-" + "a" * 32
    headers = m.wrong_bearer_request_headers(live)
    assert headers["Authorization"] == "Bearer " + m.WRONG_BEARER
    assert live not in headers["Authorization"]
    assert headers["Authorization"] != "Bearer " + live
    assert headers["Content-Type"] == "application/json"
    assert "text/event-stream" in headers["Accept"]
    with pytest.raises(m.I4SmokeError):
        m.wrong_bearer_request_headers(m.WRONG_BEARER)


def test_i4_wrong_bearer_probe_is_fail_closed_before_the_correct_session() -> None:
    src = _SOURCE.read_text("utf-8")
    assert "await wrong_bearer_is_refused(port, token)" in src
    negative = src.index("async def wrong_bearer_is_refused(")
    probe = src.index("await wrong_bearer_is_refused(port, token)")
    session = src.index("async with ClientSession(reader, writer) as session")
    assert negative < probe < session
    body = src[negative:session]
    for required in (
        "status_code == 401",
        '"mcp-session-id" not in',
        '"result" in payload or "error" in payload',
        "json=INITIALIZE_REQUEST",
        "trust_env=False",
        "timeout=10",
    ):
        assert required in body
    assert "response.json()" not in body
    assert "print(token" not in src
    assert "print(tokens" not in src
    assert "secret" not in body


def test_i4_live_source_has_scoped_cleanup_and_production_guards() -> None:
    src = _SOURCE.read_text("utf-8")
    for required in (
        "gate.verify_task_rows(rows, require_candidates_absent=False)",
        "four_disabled(gate)",
        "protected_baseline(gate)",
        "started.append(",
        "for component, instance in reversed(started):",
        "load_native_core_bridge_binding(",
        "await client.call(",
        "CORE_NOT_AUTHORIZED",
        "new_generation(",
        "same_generation(",
        'stopped = cli("core", IDS[0], "stop")',
        'stopped = cli("bridge", IDS[0], "stop")',
        "I4_NATIVE_TWO_OWNED_PAIRS=PASS",
    ):
        assert required in src
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Stop-Process",
        "Remove-Item",
        "git push",
        "secret_hex",
        "print(tokens)",
    ):
        assert forbidden not in src
