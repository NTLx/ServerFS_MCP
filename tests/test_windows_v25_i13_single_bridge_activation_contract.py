"""I13 single native Bridge activation source-only contract.

Collection never invokes Windows Scheduler, Git, Provider or installed services.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / (
    "deployment/windows/v0250-i13-single-bridge-activation-smoke.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_activation_contract", SOURCE)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_exact_single_bridge_and_sanitized_start_reasons() -> None:
    from serverfs_mcp.windows_v25.tasks import TaskError

    mod = module()
    assert mod.INSTANCE == "i13-a"
    for phase in ("enable", "run", "ready"):
        for rollback in ("UNVERIFIED", "disabled-and-ended"):
            exc = TaskError(f"component start refused at {phase}; rollback={rollback}")
            assert mod.classify_start(exc) == f"stage={phase} rollback={rollback}"
    assert mod.classify_start(TaskError("secret unexpected path")) == "stage=OTHER_REDACTED"


def test_non_task_error_does_not_leak_exception_text() -> None:
    mod = module()
    assert mod.classify_start(ValueError("secret")) == "type=ValueError"


def test_native_oneuse_and_exact_service_only_scope() -> None:
    text = SOURCE.read_text("utf-8")
    tree = ast.parse(text)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"main", "classify_start", "require"} <= names
    for phrase in (
        'INSTANCE = "i13-a"',
        'sys.argv[1:] == ["--run-once"]',
        "current_user_sid() == storage._sid()",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "installed.postflight()",
        "cli._preflight(bridge)",
        "cli._assert_instance_isolation(bridge)",
        "assert_legacy_writable_roots_disjoint(bridge)",
        "state = scheduler.start(bridge)",
        "state = scheduler.stop(bridge)",
        "I13_BRIDGE_A_EXACT_DISABLED_CLEANUP=",
        "old.same_generation(generation, _current_identity(prod))",
        "I13_BRIDGE_A_SINGLE_OWNER_ACTIVATION_E2E=PASS",
    ):
        assert phrase in text
    for forbidden in (
        "submit_agent_task",
        "run_repo_action",
        "commit_repo_changes",
        "push_repo",
        "subprocess.run(",
        "os.system(",
        "shell=True",
        "shutil.rmtree(",
    ):
        assert forbidden not in text


def test_bad_oneuse_flag_guard_is_fail_closed() -> None:
    mod = module()
    with pytest.raises(mod.I13ActivationRefusal):
        mod.require(False, "test refusal")
