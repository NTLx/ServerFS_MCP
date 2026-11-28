"""I10 one-use three-phase durable runner contracts, source-only."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deployment/windows/v0250-i10-durable-runner.py"


def load():
    spec = importlib.util.spec_from_file_location("i10_durable_runner_test", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_distinct_mode_targets_evidence_files_and_private_interpreter() -> None:
    m = load()
    assert set(m.MODES) == {"stage", "public", "retire"}
    assert m.MODES["stage"]["target"] == "v0250-i10-same-core-dual-connectors-stage.py"
    assert m.MODES["public"]["target"] == "v0250-i10-same-core-public-bridge-smoke.py"
    assert m.MODES["retire"]["target"] == "v0250-i10-public-connector-retire.py"
    assert m.MODES["stage"]["env"] == m.MODES["retire"]["env"] == "development"
    assert m.MODES["public"]["env"] == "bridge"
    assert len({row["capture"] for row in m.MODES.values()}) == 3
    assert all(row["capture"].startswith("v0250-windows-i10-") for row in m.MODES.values())
    assert [m.MODES[x]["args"] for x in ("stage", "public", "retire")] == [
        ("--stage",),
        ("--run-once",),
        ("--retire-after-remote-restore",),
    ]


def test_stage_success_requires_postflight_and_two_distinct_token_proof() -> None:
    m = load()
    p = m.MODES["stage"]
    assert p["post"] == "--postflight"
    assert {
        "I10_TWO_DISTINCT_PRIVATE_NONPROD_TOKENS=PASS",
        "I10_BOTH_CONNECTORS_SAME_CORE_DIFFERENT_TUNNELS=PASS",
        "I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS",
        "I10_TWO_CONNECTORS_ONE_CORE_DISABLED_STAGE=PASS",
    } <= p["required"]
    assert p["post_required"] == {
        "I10_BOTH_CONNECTORS_SAME_CORE_DIFFERENT_TUNNELS=PASS",
        "I10_DISABLED_NATIVE_CONNECTOR_POSTFLIGHT=PASS",
    }


def test_public_requires_peer_stop_auth_and_four_disabled_owner_evidence() -> None:
    m = load()
    p = m.MODES["public"]
    assert p["post"] is None
    assert {
        "I10_SIGNED_ONE_CORE_SHARED_BRIDGE=PASS",
        "I10_TWO_PUBLIC_HTTPS_FOREIGN_BEARERS_DENIED=PASS",
        "I10_TWO_PUBLIC_MCP_SESSIONS_SAME_CORE_BRIDGE=PASS",
        "I10_ROUTE_B_SURVIVES_CONNECTOR_A_STOP=PASS",
        "I10_FOUR_SCOPED_TASKS_DISABLED=PASS",
        "I10_I7_I5_CORE_B_PRODUCTION_UNCHANGED=PASS",
        "I10_TWO_PUBLIC_CONNECTORS_ONE_CORE_AND_BRIDGE_E2E=PASS",
    } <= p["required"]


def test_retire_needs_independent_postflight_and_original_i7_protection() -> None:
    m = load()
    p = m.MODES["retire"]
    assert p["post"] == "--postflight"
    assert {
        "I10_TWO_TEMPORARY_CONNECTOR_OWNERS_OFFLINE=VERIFIED",
        "I10_TWO_TEMPORARY_TASKS_UNREGISTERED=PASS",
        "I10_TWO_COPIED_NONPROD_TOKENS_RETIRED=PASS",
        "I10_TEMPORARY_CONNECTOR_RETIRE=PASS",
    } <= p["required"]
    assert p["post_required"] == {
        "I10_ONLY_NATIVE_RUNTIME_EVIDENCE_RETAINED=PASS",
        "I10_I7_I5_PRODUCTION_PROTECTED_POST_RETIRE=PASS",
    }


def test_no_raw_os_paths_secrets_or_another_mode_on_failure() -> None:
    m = load()
    assert m.safe_lines(
        "\n".join(
            [
                "I10_TWO_DISTINCT_PRIVATE_NONPROD_TOKENS=PASS",
                "I10_TWO_TEMPORARY_CONNECTOR_OWNERS_OFFLINE=VERIFIED",
                "I10_SENSITIVE=Bearer secret",
                "I10_PATH=C:/Users/secret",
                "I10_UNSAFE=PASS C:/temp",
                "I13_FOREIGN=PASS",
                "Traceback (most recent call last)",
            ]
        )
    ) == (
        "I10_TWO_DISTINCT_PRIVATE_NONPROD_TOKENS=PASS",
        "I10_TWO_TEMPORARY_CONNECTOR_OWNERS_OFFLINE=VERIFIED",
    )
    source = RUNNER.read_text("utf-8")
    tree = ast.parse(source)
    functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    body = ast.get_source_segment(source, functions["main"])
    assert body is not None
    assert 'report.open("x"' in body
    assert 'if first.returncode != 0 or not plan["required"] <= set(output):' in body
    assert 'if follow.returncode != 0 or not plan["post_required"] <= set(follow_output):' in body
    assert "I10_DURABLE_RUNNER=FAILED type=" in source
    for forbidden in (
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "os.system(",
        "print(first.stdout)",
        "print(first.stderr)",
        "print(token)",
        "shell=True",
        "git push",
        "git commit",
    ):
        assert forbidden not in source
