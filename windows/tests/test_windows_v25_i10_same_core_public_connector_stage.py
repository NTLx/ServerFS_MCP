"""Source-only I10 same-Core dual nonproduction Cloudflare Connector acceptance.

The real native stage requires manual remote ingress change and is never run
by pytest. No real Scheduler, Token, Tunnel, or file mutation in tests.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "deployment/windows/v0250-i10-same-core-dual-connectors-stage.py"
I5 = ROOT / "deployment/windows/v0250-i5-provider-shared-preflight.py"


def load(path: Path, label: str):
    spec = importlib.util.spec_from_file_location(label, path)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_exact_same_Core_two_independent_named_nonprod_Tunnels() -> None:
    s = load(STAGE, "i10_source_contract")
    assert s.CORE == "i5-core-a"
    assert s.BRIDGE == "i5-shared"
    assert s.PORT == 18873
    assert s.CONNECTORS == (
        ("i10-edge-a", "i7-edge-a"),
        ("i10-edge-b", "i7-edge-b"),
    )
    assert len({c for c, _ in s.CONNECTORS}) == 2


def test_I5_inventory_allows_only_four_optional_disabled_entries() -> None:
    p = load(I5, "i10_i5_allowed_inventory")
    assert p.CONNECTOR_TARGETS == (
        "ServerFS v25 i5-core-a Cloudflare Connector i5-edge-a",
        "ServerFS v25 i5-core-b Cloudflare Connector i5-edge-b",
        "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-a",
        "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-b",
    )
    base = [{"name": n, "state": "Disabled", "enabled": False} for n in p.TARGETS]
    optional = [dict(name=n, state="Disabled", enabled=False) for n in p.CONNECTOR_TARGETS]
    p.verify_candidates(base + optional[2:], installed=True)
    for extra in (
        {
            "name": "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-C",
            "state": "Disabled",
            "enabled": False,
        },
        {
            "name": "ServerFS v25 i5-core-a Cloudflare Connector i10-edge-a",
            "state": "Ready",
            "enabled": True,
        },
    ):
        with pytest.raises(p.I5PreflightError):
            p.verify_candidates(base + [extra], installed=True)
    with pytest.raises(p.I5PreflightError):
        p.verify_candidates(base + optional + [optional[0]], installed=True)


def test_disabled_stage_is_only_temporary_same_core_nonprod_credential_copy() -> None:
    source = STAGE.read_text("utf-8")
    for required in (
        'storage.prepare("cloudflare", CORE, connector)',
        'storage.home("cloudflare", "i7-public", original)',
        "i7.private_token_record(previous, sid=sid)",
        "installer._new_private_file(token, data, sid)",
        "i7.private_token_record(token, sid=sid)",
        '"connector_id": connector',
        '"instance_id": CORE',
        '"hostname": CORE + ".local"',
        '"port": PORT',
        "storage.stage(manifest)",
        "Scheduler().install(manifest)",
        'status.get("readiness") == "offline"',
        "i5.verify_candidates(all_i5, installed=True)",
        "existing.gates()",
        "retired.postflight()",
        "I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "os.system(",
        "taskkill",
        "subprocess.Popen(",
        "ANTHROPIC_API_KEY",
        "git push",
        "git commit",
        "print(data)",
        "print(token)",
    ):
        assert forbidden not in source, forbidden


def test_native_staging_function_has_no_cloudflare_remote_mutation() -> None:
    tree = ast.parse(STAGE.read_text("utf-8"))
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    assert {"native", "baseline", "fresh", "stage", "postflight", "main"} <= set(funcs)
    source = STAGE.read_text("utf-8")
    stage = ast.get_source_segment(source, funcs["stage"])
    assert stage is not None
    calls = [f for f in ast.walk(funcs["stage"]) if isinstance(f, ast.Call)]
    methods = [c.func.attr for c in calls if isinstance(c.func, ast.Attribute)]
    assert methods.count("install") == 1
    assert "uninstall" not in methods and "start" not in methods
    assert stage.index("fresh()") < stage.index("storage.prepare(")


def test_refusal_does_not_disclose_private_tokens() -> None:
    s = load(STAGE, "i10_refusal_contract")
    s.require(True, "ok")
    with pytest.raises(s.I10Refusal):
        s.require(False, "stop")
