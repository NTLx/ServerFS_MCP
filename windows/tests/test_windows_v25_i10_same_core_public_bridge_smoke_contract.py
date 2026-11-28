"""I10 public dual-Tunnel one-Core / one-Bridge source-only contracts.

No live Cloudflare or Scheduler execution from pytest. Native acceptance is
a separate one-use test only after an independently verified tunnel handover.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deployment/windows/v0250-i10-same-core-public-bridge-smoke.py"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("i10_topology_a_contract", SOURCE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_two_public_hosts_converge_to_one_bridge_core() -> None:
    module = _load_fixture()
    assert module.CORE == "i5-core-a"
    assert module.BRIDGE == "i5-shared"
    assert module.PORT == 18873
    assert module.ROUTES == (
        ("i10-edge-a", "workpc-v25a.ntlx.top"),
        ("i10-edge-b", "workpc-v25b.ntlx.top"),
    )


def test_public_auth_and_negative_provider_boundaries_are_real_mcp_calls() -> None:
    source = SOURCE.read_text("utf-8")
    for marker in (
        "stage.postflight()",
        "public.original_i7_owners_disabled()",
        'signed.call("bridge.ping", {"workdir": "Fixture"})',
        "public.denials(hostname, key, foreign)",
        "public.public_mcp(host, key)",
        "public.public_mcp(ROUTES[1][1], key)",
        "I10_TWO_PUBLIC_HTTPS_FOREIGN_BEARERS_DENIED=PASS",
        "I10_TWO_PUBLIC_MCP_SESSIONS_SAME_CORE_BRIDGE=PASS",
        "I10_ROUTE_B_SURVIVES_CONNECTOR_A_STOP=PASS",
        "I10_I7_I5_CORE_B_PRODUCTION_UNCHANGED=PASS",
        "I10_TWO_PUBLIC_CONNECTORS_ONE_CORE_AND_BRIDGE_E2E=PASS",
    ):
        assert marker in source, marker


def test_native_scoped_cleanup_and_owned_bearer_privacy() -> None:
    source = SOURCE.read_text("utf-8")
    assert "finally:" in source
    assert 'started.append(("cloudflare", connector))' in source
    assert 'started.append(("bridge", BRIDGE))' in source
    assert 'started.append(("core", CORE))' in source
    assert "for component, instance in reversed(started):" in source
    assert "native.protected_snapshot(native.load_gate()) == protected_snapshot" in source
    for forbidden in (
        'session.call_tool("submit_agent_task"',
        "run_repo_action",
        "git commit",
        "git push",
        "cloudflare.request(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "Scheduler().delete(",
        "tunnel.token",
        "print(key)",
        "print(foreign)",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source


def test_only_exact_I10_two_connector_control_names_accepted() -> None:
    t = _load_fixture()
    assert not t.is_disabled({})
    assert t.is_disabled(
        {
            "registered": True,
            "enabled": False,
            "running": False,
            "state": "Disabled",
        }
    )
    for bad in (
        {"registered": False, "enabled": False, "running": False, "state": "Disabled"},
        {"registered": True, "enabled": True, "running": True, "state": "Ready"},
    ):
        assert not t.is_disabled(bad)
    with pytest.raises(t.I10PublicRefusal):
        t.require(False, "bad")


def test_public_gather_and_cli_args_have_independent_connector_identifiers() -> None:
    src = SOURCE.read_text("utf-8")
    parsed = ast.parse(src)
    funcs = {node.name: node for node in parsed.body if isinstance(node, ast.FunctionDef)}
    assert {"helper", "connector_cli", "main"} <= set(funcs)
    commands = [
        node
        for node in ast.walk(funcs["connector_cli"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and node.args
    ]
    assert len(commands) == 1
    argv = commands[0].args[0]
    assert isinstance(argv, ast.List)
    for flag, expected in (("--instance", "CORE"), ("--connector-id", "connector")):
        positions = [
            index
            for index, node in enumerate(argv.elts)
            if isinstance(node, ast.Constant) and node.value == flag
        ]
        assert len(positions) == 1
        value = argv.elts[positions[0] + 1]
        assert isinstance(value, ast.Name) and value.id == expected
    assert 'action in ("start", "stop", "status")' in src
    assert "async def two_public_sessions(public, key: str)" in src
