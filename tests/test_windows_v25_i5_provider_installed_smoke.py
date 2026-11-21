"""Safety contract for native I5 shared-Bridge + dual-Core *no-model* smoke."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-provider-installed-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_nomodel_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_smoke_reserves_only_exact_three_tasks() -> None:
    m = _module()
    assert m.BRIDGE == "i5-shared"
    assert m.CORES == {"i5-core-a": 18873, "i5-core-b": 18874}
    assert m.TASKS == (
        ("bridge", "i5-shared"),
        ("core", "i5-core-a"),
        ("core", "i5-core-b"),
    )


@pytest.mark.parametrize(
    ("component", "instance", "action"),
    [
        ("core", "default", "stop"),
        ("bridge", "i4-a", "stop"),
        ("cloudflare", "i5-shared", "start"),
        ("bridge", "i5-shared", "uninstall"),
        ("core", "i5-core-a", "enable"),
    ],
)
def test_i5_cli_rejects_foreign_lifecycle_operations_without_spawning(
    component: str, instance: str, action: str
) -> None:
    module = _module()
    with pytest.raises(module.I5NativeSmokeError):
        module.cli(component, instance, action)


def test_i5_smoke_never_submits_any_provider_task_or_writes_files() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "started.append(",
        "for component, instance in reversed(started):",
        'await client.call("bridge.ping"',
        'await client.call("runtime.list", {})',
        "CORE_NOT_AUTHORIZED",
        "I5_INSTALLED_TWO_AUTHENTICATED_HTTP_CORES=PASS",
        "I5_INSTALLED_FIXTURE_STOP=",
        "I5_INSTALLED_PRODUCTION_AND_I1_I4=UNCHANGED",
        "I5_NATIVE_NO_MODEL_BRIDGE_AND_TWO_CORE=PASS",
    ):
        assert required in source
    for forbidden in (
        'client.call("task.submit"',
        'client.call("task.cancel"',
        '"submit_agent_task", {',
        '"create_text_file", {',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "git push",
        "git commit",
        "ANTHROPIC_API_KEY",
        "secret_hex",
        ".write_text(",
        ".write_bytes(",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            b"windows-v25: component start refused at enable; rollback=disabled-and-ended",
            "START_ENABLE",
        ),
        (
            b"windows-v25: component start refused at run; rollback=disabled-and-ended",
            "START_RUN",
        ),
        (
            b"windows-v25: component start refused at ready; rollback=disabled-and-ended",
            "START_READY",
        ),
        (
            b"windows-v25: registered service task does not match owner contract",
            "SCHEDULER_OWNER_XML",
        ),
        (
            b"windows-v25: Scheduler current-user state query is unavailable",
            "SCHEDULER_QUERY",
        ),
        (
            b"windows-v25: service refused (BridgeV2OwnerError)",
            "NATIVE_PREFLIGHT_EXCEPTION",
        ),
        (b"PRIVATE TOKEN MUST NEVER APPEAR", "UNKNOWN"),
    ],
)
def test_i5_cli_refusal_emits_only_a_fixed_category(raw: bytes, expected: str) -> None:
    assert _module().classify_cli_refusal(raw) == expected


def test_i5_bridge_preflight_is_checked_before_any_service_start() -> None:
    source = _SOURCE.read_text("utf-8")
    for marker in (
        "I5_BRIDGE_START_PRIVATE_PREFLIGHT=PASS",
        "I5_BRIDGE_START_INSTALLED_PEER_ISOLATION=PASS",
        "I5_BRIDGE_START_LEGACY_DISJOINTNESS=PASS",
        "I5_SCOPED_CLI_REFUSAL=",
    ):
        assert marker in source
    assert source.index('PHASE = "bridge-start-static-preflight"') < source.index(
        'PHASE = "isolated-bridge-start"'
    )


def test_i5_smoke_only_uses_nonsecret_status_and_fixed_error_class() -> None:
    module = _module()
    with pytest.raises(module.I5NativeSmokeError):
        module.require(False, "must refuse")
    module.require(True, "allowed")
