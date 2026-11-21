"""No-side-effect source/marker acceptance for the I14 durable postflight runner."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deployment/windows/v0250-i14-postflight-capture.py"


def load():
    spec = importlib.util.spec_from_file_location("i14_exact_postflight_capture", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_only_exact_protected_i14_disabled_postflight_markers_are_required() -> None:
    mod = load()
    assert mod.TARGET == "v0250-i14-two-owned-repo-public-connector-stage.py"
    assert mod.REQUIRED == {
        "I14_TWO_CONNECTORS_INDEPENDENT_OWNED_CORES=PASS",
        "I14_PRIVATE_DISABLED_CONNECTOR_POSTFLIGHT=PASS",
    }
    assert mod.EVIDENCE == "v0250-windows-i14-disabled-postflight-capture-2026-11-17.txt"


def test_secret_or_path_bearing_output_is_discarded() -> None:
    mod = load()
    raw = "\n".join(
        (
            "I14_PRIVATE_DISABLED_CONNECTOR_POSTFLIGHT=PASS",
            "I13_PRODUCTION_AND_I4_I11=UNCHANGED",
            "I7_TWO_CONNECTOR_TASKS_DISABLED_OFFLINE=PASS",
            "I14_CREDENTIAL=secret-value",
            "I14_BEARER=C:/secret/token",
            "I14_SECURITY=FAILED phase=unsafe",
            "Traceback (most recent call last):",
            "I14_TEST=PASS/" + "x" * 170,
            "A_USER_PRIVATE_DIRECTORY=C:/No",
        )
    )
    assert mod.safe_lines(raw) == (
        "I14_PRIVATE_DISABLED_CONNECTOR_POSTFLIGHT=PASS",
        "I13_PRODUCTION_AND_I4_I11=UNCHANGED",
        "I7_TWO_CONNECTOR_TASKS_DISABLED_OFFLINE=PASS",
    )


def test_durable_runner_does_not_execute_any_mutating_i14_command() -> None:
    source = RUNNER.read_text("utf-8")
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "main" in functions and "safe_lines" in functions
    assert '"--postflight"' in source
    assert '"--capture-once"' in source
    assert 'output.open("x"' in source
    assert "child.returncode == 0 and REQUIRED <= set(markers)" in source
    assert "os.fsync(record.fileno())" in source
    assert "sys.executable).resolve(strict=True) != expected" in source
    for forbidden in (
        '"--stage"',
        '"--run-once"',
        '"--retire-after-verified-remote-restore"',
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "git commit",
        "git push",
        "subprocess.Popen(",
        "shell=True",
    ):
        assert forbidden not in source
