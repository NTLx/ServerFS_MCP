"""Legacy/i5 shared writable workdir overlap diagnosis contracts only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.workdir_ownership import (
    WorkdirClaim,
    WorkdirOwnershipError,
    assert_independent_bridge_roots,
)

_I5_ROOT = (
    r"C:\Users\owner\AppData\Local\ServerFS"
    r"\v25\instances\i5-shared\workdirs\fixture"
)


_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-legacy-overlap-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_legacy_overlap_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_workdir_ownership_allows_disjoint_writable_roots() -> None:
    a = WorkdirClaim("i5-shared", _I5_ROOT, True)
    legacy = WorkdirClaim("default", r"D:\Downloads\Project", True)
    assert_independent_bridge_roots((a,), (legacy,))


@pytest.mark.parametrize(
    "legacy",
    [
        r"C:\Users\owner\AppData\Local\ServerFS",
        _I5_ROOT,
        _I5_ROOT + r"\child",
    ],
)
def test_workdir_ownership_rejects_writable_ancestor_equal_and_descendant(legacy: str) -> None:
    a = WorkdirClaim("i5-shared", _I5_ROOT, True)
    b = WorkdirClaim("default", legacy, False)
    with pytest.raises(WorkdirOwnershipError):
        assert_independent_bridge_roots((a,), (b,))


def test_legacy_overlap_diagnostic_proves_task_owner_and_config_before_decision() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "_legacy_bridge_active() is True",
        "load_owned_bridge_workdir_claims(installed)",
        "storage.check_private(private_fixture",
        "_manifest_document()",
        "_query_task_xml()",
        "validate_registered_task(xml, legacy)",
        "_roots(Path(path))",
        "assert_independent_bridge_roots(proposed, legacy_claims)",
        "I5_LEGACY_OVERLAP_READONLY=PASS",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.prepare(",
        "storage.stage(",
        "os.replace(",
        "os.system(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "git push",
        "git commit",
        "secret_hex",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source


def test_legacy_overlap_refusal_is_strict() -> None:
    module = _module()
    module.require(True, "ok")
    with pytest.raises(module.I5LegacyOverlapError):
        module.require(False, "denied")
