"""I11 two new Windows owned Agent pair read-only preflight source contracts.

No private fixture, Scheduler changes, model invocation or network IO in tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i11-two-owned-provider-preflight.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i11_owned_provider_preflight_contract", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_fresh_two_pair_names_and_loopback_ports() -> None:
    m = _module()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.PORTS == {"i11-a": 18881, "i11-b": 18882}
    assert m.TASK_NAMES == {
        "ServerFS v25 i11-a Core",
        "ServerFS v25 i11-a Agent Bridge",
        "ServerFS v25 i11-b Core",
        "ServerFS v25 i11-b Agent Bridge",
    }


def test_readonly_preflight_fails_closed_on_occupied_scheduler_namespace() -> None:
    m = _module()
    m.verify_tasks([], installed=False)
    disabled = [
        {"name": name, "state": "Disabled", "enabled": False} for name in sorted(m.TASK_NAMES)
    ]
    m.verify_tasks(disabled, installed=True)
    for wrong in (
        disabled[:3],
        [*disabled, disabled[0]],
        [*disabled[:3], {**disabled[-1], "state": "Ready", "enabled": True}],
        [*disabled[:3], {**disabled[-1], "name": "ServerFS v25 i11-z Core"}],
    ):
        with pytest.raises(m.I11PreflightRefusal):
            m.verify_tasks(wrong, installed=True)
    with pytest.raises(m.I11PreflightRefusal):
        m.verify_tasks(disabled, installed=False)


def test_preflight_native_safety_guards_no_mutating_actions() -> None:
    source = SCRIPT.read_text("utf-8")
    for expected in (
        "Get-ScheduledTask -ErrorAction Stop",
        "_.TaskName -like 'ServerFS v25 i11-*'",
        "gate.verify_task_rows(gate.scheduler_rows(), require_candidates_absent=False)",
        "i7.postflight()",
        "verify_tasks(candidate_tasks(), installed=False)",
        "not root.exists() and not root.is_symlink()",
        "not any(row.port in PORTS.values() for row in listeners)",
        'shutil.which("claude")',
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "current_user_sid() == storage._sid()",
        "I11_TWO_OWNED_PROVIDER_PAIRS_PREFLIGHT=PASS",
    ):
        assert expected in source, expected
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "storage.prepare(",
        "storage.stage(",
        "cloudflare.request(",
        "subprocess.Popen(",
        "ANTHROPIC_API_KEY",
        "print(claude)",
        "print(root)",
        "git push",
        "git commit",
    ):
        assert forbidden not in source, forbidden


def test_nonsecret_failure_class() -> None:
    m = _module()
    m.require(True, "accepted")
    with pytest.raises(m.I11PreflightRefusal):
        m.require(False, "refused")
