"""W2h private-only Windows disabled task staging: pure source contract tests.

All tests run without Windows native APIs or local service-manager mutation.
The real Windows gate requires a separate normal-host FullLanguage report.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deployment/windows/v0250-w2h-stage-disabled.py"
_AUDIT = _ROOT / "deployment/windows/v0250-w2h-input-audit.py"
_NATIVE_WRAPPER = _ROOT / "deployment/windows/v0250-w2h-stage-disabled.ps1"
_PREFLIGHT_WRAPPER = _ROOT / "deployment/windows/v0250-w2h-host-preflight.ps1"


def _load_staging():
    spec = importlib.util.spec_from_file_location("w2h_staging_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [_SCRIPT, _AUDIT])
def test_w2h_installer_and_audit_have_valid_python_syntax(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_four_component_tasks_have_distinct_disabled_staging_interpreters() -> None:
    script = _load_staging()
    assert script._TASKS == (
        ("core", "core-owner-env", "ServerFS MCP Core"),
        ("bridge", "bridge-owner-env", "ServerFS Agent Bridge"),
        ("cloudflare", "cloudflare-connector-env", "ServerFS Cloudflare Connector"),
        ("openai", "openai-connector-env", "ServerFS OpenAI Connector"),
    )
    assert len({entry[1] for entry in script._TASKS}) == 4
    assert len(script._ENV_NAMES) == 5
    assert script._CORE_PORT != 18765
    assert script._CORE_PORT == 18766
    assert script._HOSTNAME == "workpc-serverfs.ntlx.top"


def test_preflight_is_separate_hard_gate_before_installation() -> None:
    operator = _NATIVE_WRAPPER.read_text(encoding="utf-8")
    preflight = _PREFLIGHT_WRAPPER.read_text(encoding="utf-8")
    assert operator.index("& $preflight") < operator.index("& $python")
    assert "Get-ScheduledTask" in operator
    assert "Get-ScheduledTask" in preflight
    assert "Write-Output ('W2h host preflight report:" in preflight
    assert "start()" not in operator
    assert "restart()" not in operator


def test_staging_never_contains_legacy_service_mutation_commands() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert '"install"' in source
    assert '"status"' in source
    assert '"restart"' not in source
    assert '"uninstall"' not in source
    assert '"stop"' not in source
    assert "shutil.copyfileobj" in source
    assert "hardlink_to" not in source
    assert "secrets.token_urlsafe" in source
    assert "W2h partial stage" in source


def test_source_digests_are_explicit_and_candidate_is_not_release() -> None:
    script = _load_staging()
    assert len(script._TUNNEL_SHA) == 64
    assert script._TUNNEL_SHA.isalnum()
    assert script._TUNNEL_ID.fullmatch("tunnel_" + "a" * 32)
    assert script._TUNNEL_ID.fullmatch("tunnel_" + "b" * 31) is None
