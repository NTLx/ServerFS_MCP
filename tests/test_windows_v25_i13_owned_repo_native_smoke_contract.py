"""Source-only safety contract for I13 two real independent Repo Ops Windows canary.

Collection never starts Windows tasks, opens credentials, executes Git or Provider.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-owned-repo-native-smoke.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_real_repo_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_exact_two_owned_repo_only_candidates() -> None:
    mod = module()
    assert mod.IDS == ("i13-a", "i13-b")
    assert mod.PORTS == {"i13-a": 18883, "i13-b": 18884}
    assert set(mod.BODIES) == set(mod.IDS)
    assert len(set(mod.BODIES.values())) == 2
    assert mod.ACTION == "status_probe"
    with pytest.raises(mod.I13RepoRefusal):
        mod.require(False, "private diagnostic refusal")


def test_cleanup_deletes_only_exact_private_marker(tmp_path: Path) -> None:
    mod = module()
    name = "i13-owned-repo-known.txt"
    root = tmp_path
    original = root / name
    body = mod.BODIES["i13-a"]
    original.write_bytes(body.encode("ascii"))
    assert mod.remove_exact_marker(root, name, body)
    assert not original.exists()
    assert mod.remove_exact_marker(root, name, body)


def test_cleanup_refuses_foreign_or_modified_marker(tmp_path: Path) -> None:
    mod = module()
    name = "i13-owned-repo-known.txt"
    marker = tmp_path / name
    marker.write_bytes(b"FOREIGN_BYTES\n")
    with pytest.raises(mod.I13RepoRefusal):
        mod.remove_exact_marker(tmp_path, name, mod.BODIES["i13-b"])
    assert marker.read_bytes() == b"FOREIGN_BYTES\n"
    other = tmp_path / "other.txt"
    other.write_bytes(b"FOREIGN_BYTES\n")
    with pytest.raises(mod.I13RepoRefusal):
        mod.remove_exact_marker(tmp_path, "other.txt", mod.BODIES["i13-a"])
    assert other.read_bytes() == b"FOREIGN_BYTES\n"


def test_native_isolation_and_scope_are_explicit() -> None:
    source = SCRIPT.read_text("utf-8")
    required = (
        "stage.postflight()",
        "old.protected_baseline(gate)",
        "current_user_sid() == storage._sid()",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        'sys.argv[1:] == ["--run-once"]',
        '"Authorization": "Bearer " + other',
        "result.status_code == 401",
        '"inspect_repo", "run_repo_action", "create_text_file", "list_workdirs"',
        '"submit_agent_task" not in names',
        '"list_agent_runtimes" not in names',
        '"agent_mode") == "disabled"',
        '"repo_ops") is True',
        '"action": ACTION',
        '"action": "unconfigured_action"',
        'runtime.cli("core", IDS[0], "stop")',
        "runtime.same(a, b)",
        "asyncio.gather(",
        "asyncio.wait_for(",
        "remove_exact_marker(",
        "I13_TWO_INDEPENDENT_OWNED_REPO_ONLY_NATIVE_E2E=PASS",
    )
    for item in required:
        assert item in source, item
    forbidden = (
        '"commit_repo_changes"',
        '"push_repo"',
        '"submit_agent_task", {',
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "shutil.rmtree(",
        "os.system(",
        "shell=True",
        "os.kill(",
        "subprocess.run(",
    )
    for item in forbidden:
        assert item not in source, item


def test_main_never_leaks_raw_exception_message_or_secret() -> None:
    source = SCRIPT.read_text("utf-8")
    assert '"I13_REAL_REPO=FAILED phase=" + PHASE + " type=" + type(exc).__name__' in source
    assert "print(exc)" not in source
    assert "traceback.print_exc" not in source
    tree = ast.parse(source)
    names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"main", "inspect_one", "survivor", "both_repos", "remove_exact_marker"} <= names
