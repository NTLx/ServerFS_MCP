"""Offline security contracts for I7 public dual HTTPS/MCP acceptance canary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-public-dual-https-smoke.py"
)
SPEC = importlib.util.spec_from_file_location("i7_public_dual_https_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)


def test_fixed_domains_and_isolated_instance() -> None:
    assert canary.INSTANCE == "i7-public"
    assert canary.CONNECTORS == ("i7-edge-a", "i7-edge-b")
    assert canary.HOSTS == ("workpc-v25a.ntlx.top", "workpc-v25b.ntlx.top")
    assert canary.PORT == 18875
    assert canary._initialization()["method"] == "initialize"


def test_no_uncontrolled_scheduler_or_cloudflare_mutation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "gate = load_helper(",
        "gate.postflight()",
        "finally:",
        "reversed(started)",
        "native.offline(",
        "_current_identity(prod)",
        "I7_PUBLIC_PRODUCTION_CORE_GENERATION=UNCHANGED",
        "I7_PUBLIC_FINALLY_ALL_THREE_DISABLED=",
        "check_public_denials",
        "check_public_mcp",
        "await asyncio.gather(",
        "trust_env=False",
        "follow_redirects=False",
        "I7_PUBLIC_TWO_HTTPS_MCP_ROUTES_E2E=PASS",
    ):
        assert fragment in text
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "dangerouslyDisableSandbox",
        "verify=False",
        "token.read_bytes(",
        "token.read_text(",
        "git push",
    ):
        assert forbidden not in text


@pytest.mark.parametrize("host", ["workpc-v25a.ntlx.top", "workpc-v25b.ntlx.top"])
def test_public_hostnames_only_select_expected_nonprod_routes(host: str) -> None:
    assert host in canary.HOSTS
    assert host.startswith("workpc-v25")
    assert host.endswith(".ntlx.top")


def test_bearer_headers_never_encoded_in_url() -> None:
    headers = canary._base_headers("private-test-fixture")
    assert headers["Authorization"] == "Bearer private-test-fixture"
    assert not any("private-test-fixture" in h for h in canary.HOSTS)
    assert headers["Accept"] == "application/json, text/event-stream"
