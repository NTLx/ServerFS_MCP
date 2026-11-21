"""Static source contracts for I7 installed Core localhost acceptance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i7-nonprod-core-installed-smoke.py"
)
_SPEC = importlib.util.spec_from_file_location("i7_nonprod_installed_smoke_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def test_i7_runtime_smoke_scopes_every_lifecycle_action_to_one_core() -> None:
    assert smoke.INSTANCE == "i7-public"
    assert smoke.HOST == "i7-public.local"
    assert smoke.PORT == 18875
    body = _SCRIPT.read_text(encoding="utf-8")
    for marker in (
        'require(action in ("start", "stop", "status")',
        "started = True",
        'state = cli("stop")',
        "gate.postflight()",
        "v0250-i7-connectors-disabled-register.py",
        "I7_PRIVATE_CORE_STOP=",
        "I7_PRODUCTION_CORE_GENERATION=UNCHANGED",
        "I7_NATIVE_INSTALLED_FILE_ONLY_CORE_HTTP=PASS",
    ):
        assert marker in body
    assert '"core", action, "--instance", INSTANCE' in " ".join(body.split())
    for forbidden in (
        'cli("bridge")',
        'cli("cloudflare")',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "cloudflared tunnel",
        "token_file",
        "git push",
    ):
        assert forbidden not in body


def test_i7_smoke_denies_unauthenticated_requests_and_host_origin_spoof() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "wrong.status_code == 401",
        "missing.status_code == 401",
        "wrong_host.status_code == 421",
        "origin.status_code != 200",
        'fixture.get("access") == "read-only"',
        'fixture.get("repo_ops") is False',
        'fixture.get("agent_mode") == "disabled"',
    ):
        assert marker in source


@pytest.mark.parametrize("action", ["restart", "uninstall", "enable", "cloudflare"])
def test_i7_scoped_cli_never_accepts_foreign_actions(action: str) -> None:
    with pytest.raises(smoke.I7SmokeError, match="only i7 Core actions"):
        smoke.cli(action)


def test_i7_production_generation_comparison_rejects_restarts() -> None:
    before = SimpleNamespace(pid=1414, creation_time=1234.0)
    assert smoke.same_generation(before, SimpleNamespace(pid=1414, creation_time=1234.0))
    assert not smoke.same_generation(before, SimpleNamespace(pid=1414, creation_time=1235.0))
    assert not smoke.same_generation(before, SimpleNamespace(pid=1415, creation_time=1234.0))
    assert not smoke.same_generation(before, None)
