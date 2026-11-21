"""I14 scoped temporary Connector retirement evidence contract (source only)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deployment/windows/v0250-i14-retirement-capture.py"


def load():
    spec = importlib.util.spec_from_file_location("i14_retire_capture_contract", RUNNER)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_exact_scoped_actions_and_required_native_cleanup() -> None:
    mod = load()
    assert mod.TARGET == "v0250-i14-two-owned-repo-public-connector-retire.py"
    assert mod.EVIDENCE == "v0250-windows-i14-two-owned-public-retire-capture-2026-11-17.txt"
    assert mod.REQUIRED == {
        "I14_TWO_NATIVE_TEMPORARY_CONNECTORS_DISABLED=VERIFIED",
        "I14_TWO_TEMPORARY_SCHEDULER_TASKS_UNREGISTERED=PASS",
        "I14_TWO_COPIED_TEST_ONLY_TOKENS_REMOVED=PASS",
        "I14_ONLY_SID_PRIVATE_RUNTIME_EVIDENCE_RETAINED=PASS",
        "I14_I13_I7_PRODUCTION_UNCHANGED=PASS",
        "I14_TEMPORARY_TWO_CONNECTORS_RETIRED=PASS",
    }


def test_sanitization_only_allows_fixed_nonsecret_markers() -> None:
    m = load()
    assert m.safe_lines(
        "\n".join(
            (
                "I14_TWO_TEMPORARY_SCHEDULER_TASKS_UNREGISTERED=PASS",
                "I14_TWO_NATIVE_TEMPORARY_CONNECTORS_DISABLED=VERIFIED",
                "I14_SECRET=Bearer secret",
                "I14_TOKEN_PATH=C:/Users/x/test.token",
                "I14_RICH_DATA=PASS path C:/",
                "I5_OTHER=PASS",
            )
        )
    ) == (
        "I14_TWO_TEMPORARY_SCHEDULER_TASKS_UNREGISTERED=PASS",
        "I14_TWO_NATIVE_TEMPORARY_CONNECTORS_DISABLED=VERIFIED",
    )


def test_runner_makes_one_irreversible_call_then_separate_read_only_postflight() -> None:
    source = RUNNER.read_text("utf-8")
    tree = ast.parse(source)
    funcs = {fn.name: fn for fn in tree.body if isinstance(fn, ast.FunctionDef)}
    assert "main" in funcs
    for required in (
        '"--capture-once"',
        '"--retire-after-verified-remote-restore"',
        '"--postflight"',
        'output.open("x"',
        "retired.returncode != 0 or not REQUIRED <= set(results)",
        "checked.returncode == 0 and POST <= set(markers)",
        "I14_RETIRE_DURABLE_EVIDENCE=",
    ):
        assert required in source
    assert source.count('"--retire-after-verified-remote-restore"') == 1
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "cloudflare.request(",
        "git push",
        "git commit",
        '"--stage"',
        '"--run-once"',
        "shell=True",
    ):
        assert forbidden not in source
