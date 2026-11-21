"""Source-only one-use I13 v2 Repo-only evidence runner contract."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-repo-v2-runner.py"


def module():
    spec = importlib.util.spec_from_file_location("i13_repo_v2_runner", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_fixed_runner_target_and_durable_capture_only() -> None:
    mod = module()
    assert mod.TARGET == "v0250-i13-repo-v2-native-smoke.py"
    assert mod.EVIDENCE == "v0250-windows-i13-two-owned-repo-v2-capture-2026-11-17.txt"
    src = SOURCE.read_text("utf-8")
    for required in (
        'storage.root("i13-a")',
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        'sys.argv[1:] == ["--run-once"]',
        "current_user_sid() == storage._sid()",
        'output.open("x"',
        "I13_REPO_V2_CAPTURE=STARTED",
        "I13_REPO_V2_CHILD_EXIT=",
        "subprocess.run(",
        "capture_output=True",
        "os.fsync(",
        "safe_lines(",
    ):
        assert required in src
    for forbidden in (
        "shell=True",
        "shutil.rmtree(",
        "os.system(",
        "commit_repo_changes",
        "push_repo",
        "submit_agent_task",
    ):
        assert forbidden not in src
    ast.parse(src)


def test_safe_lines_keeps_fixed_status_not_foreign_data() -> None:
    mod = module()
    contents = (
        "I13_REPO_V2_OPERATION=FAILED phase=start-core-i13-a type=I13RepoV2Refusal\n"
        "I13_REPO_V2_TWO_INDEPENDENT_NATIVE_REPO_E2E=PASS\n"
        "I13_REPO_V2_SECRETS=C:\\private\\secret\n"
    )
    assert mod.safe_lines(contents) == (
        "I13_REPO_V2_OPERATION=FAILED phase=start-core-i13-a type=I13RepoV2Refusal",
        "I13_REPO_V2_TWO_INDEPENDENT_NATIVE_REPO_E2E=PASS",
    )


def test_main_entrypoint_preserves_child_exit_code(capsys) -> None:
    import sys

    # Execute only the entrypoint AST with a stubbed main; never touch Windows.
    tree = ast.parse(SOURCE.read_text("utf-8"))
    entry = next(
        item
        for item in tree.body
        if isinstance(item, ast.If) and ast.unparse(item.test) == "__name__ == '__main__'"
    )
    entry_module = ast.fix_missing_locations(ast.Module(body=[entry], type_ignores=[]))
    compiled = compile(entry_module, str(SOURCE), "exec")

    for code in (0, 3):
        env = {"__name__": "__main__", "main": lambda rc=code: rc, "sys": sys}
        try:
            exec(compiled, env)
        except SystemExit as exc:
            assert exc.code == code
        else:
            raise AssertionError("runner must exit explicitly")
        assert "RUNNER=FAILED" not in capsys.readouterr().err

    def fail():
        raise ValueError("sensitive text")

    env = {"__name__": "__main__", "main": fail, "sys": sys}
    try:
        exec(compiled, env)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("runner must fail closed")
    err = capsys.readouterr().err
    assert "I13_REPO_V2_RUNNER=FAILED type=ValueError" in err
    assert "sensitive text" not in err
