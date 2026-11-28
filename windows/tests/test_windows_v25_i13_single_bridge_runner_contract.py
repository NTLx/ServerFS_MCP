"""Source-only contract of one-use I13 Bridge A durable evidence runner."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / (
    "deployment/windows/v0250-i13-single-bridge-runner.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_activation_runner", SOURCE)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_safe_marker_allowlist_and_no_secret_passthrough() -> None:
    mod = module()
    log = "\n".join(
        [
            "I13_BRIDGE_A_SINGLE_NATIVE_START=PASS",
            "I13_BRIDGE_A_SINGLE_NATIVE_START=FAILED stage=enable rollback=disabled-and-ended",
            "I13_BRIDGE_A_ACTIVATION=FAILED "
            "phase=single-bridge-a-native-activation type=I13ActivationRefusal",
            "I13_BAD=C:\\private\\secret",
            "I13_BAD=token:123",
            "I13_BAD=" + "x" * 205,
            "Path to private stuff is sensitive",
        ]
    )
    got = mod.safe_lines(log)
    assert len(got) == 3
    assert got[0] == "I13_BRIDGE_A_SINGLE_NATIVE_START=PASS"
    assert "stage=enable" in got[1]
    assert "phase=single-bridge-a-native-activation" in got[2]


def test_single_instance_durable_evidence_runner_contract() -> None:
    mod = module()
    assert mod.TARGET == "v0250-i13-single-bridge-activation-smoke.py"
    assert mod.EVIDENCE == "v0250-windows-i13-bridge-a-activation-capture-2026-11-17.txt"
    source = SOURCE.read_text("utf-8")
    for phrase in (
        'sys.argv[1:] == ["--run-once"]',
        "current_user_sid() == storage._sid()",
        'storage.root("i13-a")',
        'output.open("x"',
        "I13_ACTIVATION_CAPTURE=STARTED",
        "I13_ACTIVATION_CHILD_EXIT=",
        "subprocess.run(",
        "subprocess.DEVNULL",
        "capture_output=True",
        "safe_lines(",
        "os.fsync(",
        'creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)',
    ):
        assert phrase in source, phrase
    for forbidden in (
        "shell=True",
        "os.system(",
        "submit_agent_task",
        "commit_repo_changes",
        "push_repo",
        "run_repo_action",
        "shutil.rmtree(",
        "os.kill(",
    ):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    found = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert found >= {"main", "safe_lines"}


def test_main_entrypoint_preserves_child_exit_code(capsys) -> None:
    import sys

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
    assert "I13_ACTIVATION_RUNNER=FAILED type=ValueError" in err
    assert "sensitive text" not in err
