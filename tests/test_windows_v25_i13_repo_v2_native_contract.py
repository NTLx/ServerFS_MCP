"""Source-only contract for native I13 v2 fixed read-only Repo Ops canary."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-repo-v2-native-smoke.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_repo_v2_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_two_exact_private_owners_and_probes() -> None:
    mod = module()
    assert mod.IDS == ("i13-a", "i13-b")
    assert mod.PORTS == {"i13-a": 18883, "i13-b": 18884}
    with pytest.raises(mod.I13RepoV2Refusal):
        mod.require(False, "expected fail-closed gate")


def test_native_disabled_status_never_conflates_readiness() -> None:
    mod = module()
    expected = {
        "registered": True,
        "enabled": False,
        "state": "Disabled",
        "running": False,
    }
    assert mod.disabled(expected)
    for field, different in (
        ("registered", False),
        ("enabled", True),
        ("state", "Ready"),
        ("running", True),
    ):
        row = dict(expected)
        row[field] = different
        assert not mod.disabled(row)


def test_repo_operations_remain_readonly_and_scope_exact() -> None:
    source = SOURCE.read_text("utf-8")
    for value in (
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        'sys.argv[1:] == ["--run-once"]',
        "current_user_sid() == storage._sid()",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "stage.postflight()",
        "cli._preflight(manifest)",
        "cli._assert_instance_isolation(manifest)",
        "assert_legacy_writable_roots_disjoint(manifest)",
        "scheduler.start(manifest)",
        "scheduler.stop(manifest)",
        '"action": "status_probe"',
        '"action": "unknown_command"',
        "unauthorized.status_code == 401",
        'result.get("mutates") is False',
        "asyncio.gather(",
        "I13_REPO_V2_OPERATION=FAILED phase=",
        "I13_REPO_V2_ALL_ATTEMPTED_OWNERS_STOPPED=",
        "I13_REPO_V2_TWO_INDEPENDENT_NATIVE_REPO_E2E=PASS",
    ):
        assert value in source, value
    for forbidden in (
        '"create_text_file"',
        '"delete_file"',
        '"commit_repo_changes"',
        '"push_repo"',
        '"submit_agent_task", {',
        "shutil.rmtree(",
        "os.system(",
        "shell=True",
        "subprocess.run(",
        "os.kill(",
    ):
        assert forbidden not in source, forbidden


def test_error_provenance_is_preserved_before_cleanup() -> None:
    source = SOURCE.read_text("utf-8")
    tree = ast.parse(source)
    names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"probe", "both", "disabled", "main", "helper"} <= names
    assert source.index("I13_REPO_V2_OPERATION=FAILED phase=") < source.index(
        "I13_REPO_V2_ALL_ATTEMPTED_OWNERS_STOPPED="
    )
