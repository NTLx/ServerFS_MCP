"""Contracts for the I7 native dual real Cloudflare Connector canary.

These tests never start Windows Task Scheduler, Core, or cloudflared.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-dual-connector-live-smoke.py"
)
SPEC = importlib.util.spec_from_file_location("i7_dual_real_canary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def test_fixed_i7_identity_and_production_safe_lifecycle() -> None:
    assert canary.INSTANCE == "i7-public"
    assert canary.CONNECTORS == ("i7-edge-a", "i7-edge-b")
    code = SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        'action in ("start", "stop", "status")',
        'component in ("core", "cloudflare")',
        "connector in CONNECTORS",
        '"--instance"',
        '"--connector-id", connector',
        'started.append(("core", None))',
        'started.append(("cloudflare", connector))',
        "finally:",
        "for component, connector in reversed(started):",
        "registered.postflight()",
        "I7_DUAL_LIVE_PEER_SURVIVES_A_STOP=PASS",
        "I7_DUAL_LIVE_PRODUCTION_GENERATION=UNCHANGED",
    ):
        assert fragment in code
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        'cli("bridge"',
        "cloudflare.request(",
        "token.read_text(",
        "token.read_bytes(",
        "git push",
        "dangerouslyDisableSandbox",
    ):
        assert forbidden not in code


@pytest.mark.parametrize(
    ("component", "action", "connector"),
    [
        ("bridge", "start", None),
        ("cloudflare", "restart", "i7-edge-a"),
        ("cloudflare", "start", "foreign-id"),
        ("cloudflare", "stop", None),
        ("core", "start", "i7-edge-a"),
    ],
)
def test_cli_rejects_foreign_components_actions_and_connectors(
    component: str, action: str, connector: str | None
) -> None:
    with pytest.raises(canary.I7DualRefusal):
        canary.cli(component, action, connector)


def test_cli_calls_only_installed_i7_private_component(monkeypatch: pytest.MonkeyPatch) -> None:
    from serverfs_mcp.windows_v25 import storage

    root = Path("I7_TEST_ROOT")
    calls: list[list[str]] = []
    monkeypatch.setattr(storage, "root", lambda instance: root)
    monkeypatch.setattr(storage, "_sid", lambda: "test-sid")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        assert kwargs["stdin"] is not None
        return SimpleNamespace(returncode=0, stdout=b'{"running":true,"readiness":"owner_alive"}')

    monkeypatch.setattr(canary.subprocess, "run", fake_run)
    assert canary.cli("cloudflare", "start", "i7-edge-b")["running"] is True
    assert len(calls) == 1
    assert calls[0] == [
        str(root / "envs/cloudflare/Scripts/python.exe"),
        "-I",
        "-B",
        "-m",
        "serverfs_mcp.windows_v25.cli",
        "cloudflare",
        "start",
        "--instance",
        "i7-public",
        "--connector-id",
        "i7-edge-b",
    ]


def test_strict_offline_and_readiness_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {
        "registered": True,
        "running": False,
        "enabled": False,
        "state": "Disabled",
        "readiness": "offline",
    }
    monkeypatch.setattr(canary, "cli", lambda *args: dict(state))
    assert canary.offline("core") is True
    state["enabled"] = True
    assert canary.offline("core") is False
    state["enabled"] = False
    state["state"] = "Ready"
    assert canary.offline("cloudflare", "i7-edge-a") is False
    state.update(state="Running", running=True, readiness="owner_alive")
    assert canary.active("cloudflare", "i7-edge-b") is True
    assert canary.active("core") is False
