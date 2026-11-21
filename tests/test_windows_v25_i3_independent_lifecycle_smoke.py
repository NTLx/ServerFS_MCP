"""Source-only I3 independent Bridge/Core lifecycle acceptance contracts.

No Task Scheduler, private Windows instance, worker, credentials or network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i3-independent-lifecycle-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i3_restarts_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_generation_comparison_rejects_absent_and_reused_pid() -> None:
    mod = _module()
    a = SimpleNamespace(pid=123, creation_time=100)
    same = SimpleNamespace(pid=123, creation_time=100)
    replaced = SimpleNamespace(pid=123, creation_time=200)
    other = SimpleNamespace(pid=456, creation_time=300)
    assert mod.same_generation(a, same)
    assert not mod.new_generation(a, same)
    assert mod.new_generation(a, replaced)
    assert mod.new_generation(a, other)
    assert not mod.same_generation(a, replaced)
    assert not mod.same_generation(None, a)
    assert not mod.new_generation(None, a)
    assert not mod.new_generation(a, None)


def test_start_and_stop_readiness_require_exact_scope() -> None:
    mod = _module()
    records = {
        ("bridge", "i3-shared"): {"running": True, "enabled": True, "readiness": "owner_alive"},
        ("core", "i3-core-a"): {"running": True, "enabled": True, "readiness": "verified"},
    }

    def cli(component, instance, action):
        assert action == "status"
        return records[(component, instance)]

    helper = SimpleNamespace(cli=cli)
    assert mod.require_active(helper, "bridge", "i3-shared") is None
    assert mod.require_active(helper, "core", "i3-core-a") is None
    with pytest.raises(mod.I3RestartError):
        mod.require_disabled(helper, "core", "i3-core-a")
    records[("core", "i3-core-a")] = {
        "state": "Disabled",
        "running": False,
        "enabled": False,
        "readiness": "offline",
    }
    assert mod.require_disabled(helper, "core", "i3-core-a") is None
    with pytest.raises(mod.I3RestartError):
        mod.require_active(helper, "core", "i3-core-a")


def test_source_enforces_which_owner_may_be_restarted() -> None:
    src = _SOURCE.read_text("utf-8")
    for required in (
        "helper.three_task_states(preflight)",
        "initial_bridge = _current_identity(bridge)",
        "initial_a = _current_identity(cmanifests[CORE_A])",
        "initial_b = _current_identity(cmanifests[CORE_B])",
        'helper.cli("core", CORE_A, "stop")',
        'helper.cli("core", CORE_A, "start")',
        'helper.cli("bridge", BRIDGE, "stop")',
        'helper.cli("bridge", BRIDGE, "start")',
        "same_generation(initial_b, _current_identity(cmanifests[CORE_B]))",
        "new_generation(initial_bridge, current_bridge)",
        "helper.public_core_check(core_id, port, tokens[core_id])",
        "helper.shared_bridge_rpc(cbinding)",
        "for component, instance in reversed(live)",
        "I3_RESTART_FIXTURE_STOP=",
        "I3_RESTART_PRODUCTION_TASKS_AND_CORE=UNCHANGED",
        "I3_NATIVE_INDEPENDENT_LIFECYCLES=PASS",
    ):
        assert required in src

    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.stage(",
        "Stop-Process",
        "Remove-Item",
        "git push",
        "task.submit",
        "run_repo_action",
        "secret_hex",
        "print(tokens)",
    ):
        assert forbidden not in src


def test_private_helper_is_fixed_exact_i3_name(tmp_path: Path, monkeypatch) -> None:
    mod = _module()
    mock = SimpleNamespace(BRIDGE="wrong", CORE_PORTS=mod.CORE_PORTS)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))
    monkeypatch.setattr(mod.importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(mod.importlib.util, "module_from_spec", lambda _: mock)
    with pytest.raises(mod.I3RestartError, match="identities"):
        mod.original_smoke()
