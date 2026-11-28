"""Source-only one-use I14 public acceptance capture contracts."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deployment/windows/v0250-i14-public-runner.py"


def load():
    spec = importlib.util.spec_from_file_location("i14_public_capture_contract", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixed_nonproduction_public_runner_and_exact_six_passes() -> None:
    mod = load()
    assert mod.TARGET == "v0250-i14-two-owned-repo-public-smoke.py"
    assert mod.EVIDENCE == "v0250-windows-i14-two-owned-repo-public-capture-2026-11-17.txt"
    assert mod.REQUIRED == {
        "I14_SIX_INDEPENDENT_NATIVE_OWNER_TASKS=PASS",
        "I14_TWO_PUBLIC_REPO_MCP_AND_BEARER_ISOLATION=PASS",
        "I14_PUBLIC_B_REPO_SURVIVED_A_BRIDGE_CONNECTOR_STOP=PASS",
        "I14_SIX_NONPROD_OWNERS_DISABLED=PASS",
        "I14_PRODUCTION_I4_I11_AND_I7=UNCHANGED",
        "I14_TWO_INDEPENDENT_OWNED_REPO_PUBLIC_E2E=PASS",
    }


def test_bounded_capture_drops_sensitive_arbitrary_output() -> None:
    mod = load()
    assert mod.safe_lines(
        "\n".join(
            (
                "I14_SIX_INDEPENDENT_NATIVE_OWNER_TASKS=PASS",
                "I14_SIX_NONPROD_OWNERS_DISABLED=UNVERIFIED",
                "I14_SECRET=Bearer sample-secret",
                "I14_PATH=C:/Users/Secret",
                "I14_BROKEN=PASS " + "x" * 180,
                "Traceback (most recent call last)",
                "I13_WHATEVER=PASS",
            )
        )
    ) == (
        "I14_SIX_INDEPENDENT_NATIVE_OWNER_TASKS=PASS",
        "I14_SIX_NONPROD_OWNERS_DISABLED=UNVERIFIED",
    )


def test_runner_guarded_single_child_and_durable_terminal_evidence() -> None:
    src = RUNNER.read_text("utf-8")
    tree = ast.parse(src)
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    assert "main" in funcs and "safe_lines" in funcs
    for needed in (
        '"--capture-once"',
        '"--run-once"',
        'report.open("x"',
        "storage.check_private(expected, directory=False",
        "completed.returncode == 0 and REQUIRED <= set(markers)",
        "I14_PUBLIC_CAPTURE=STARTED",
        "I14_PUBLIC_CHILD_EXIT=",
        "I14_PUBLIC_DURABLE_EVIDENCE=",
        "raise SystemExit(code)",
    ):
        assert needed in src
    for banned in (
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "print(completed.stdout)",
        "print(completed.stderr)",
        "shell=True",
        "git push",
        "git commit",
    ):
        assert banned not in src
