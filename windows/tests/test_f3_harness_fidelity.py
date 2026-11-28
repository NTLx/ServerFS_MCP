"""The acceptance harness must measure the configuration a default deployment actually runs.

Three defects were found by reading the harness against the product rather than against its own
assertions, and each made a passing (or failing) result mean the wrong thing:

**The rendered executable was the runtime's name, not the product's default.**
`Lifecycle` interpolated ``f'{runtime}_bin = "{runtime}"'``, which for Qoder produced
``qoder_bin = "qoder"``. The product default is ``qodercli`` (``QoderSettings.qoder_bin``),
and on a Windows host ``qoder`` resolves to a ``cmd -> powershell.exe -> qodercli.exe``
dispatcher. Formal acceptance was therefore exercising a launch path no default deployment
uses, and any conclusion drawn from it was about the wrong executable.

**Sentinels injected after launch could never arrive.** E1 used to write proxy names into
``os.environ`` *after* ``launch()`` had created the launcher/supervisor/Bridge tree. A
child inherits its parent's environment as it was when ``Popen`` ran, so those names could
not reach the Bridge. E1 then reported "the tool saw none of them", which reads like a safe
refusal rather than a vacuous measurement -- the dangerous direction of error.

Both are pinned here at the deterministic level. Neither needs a provider or a network.

**The raw E1 arm applied the product's own scrub.** `build_runtime_environment_overlay` *is*
ServerFS's deletion policy for Qoder. The raw arm exists to answer whether a proxy variable in a
parent environment reaches a tool with no ServerFS policy in the path -- it is the control that
E2's absence is measured against. Building its options with that overlay deleted the very sentinels
the caller had installed before the subprocess was spawned, so the arm could only ever report
`non_vacuous=false`. That is the same dangerous direction as the post-launch write: it reads as a
safe refusal. The boundary is now pinned, not commented.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> Any:
    """Load an e2e helper by file location, the way D9 already loads the shared helpers.

    These modules live outside the importable package because they are acceptance scripts, not
    product code, so they are loaded by path rather than added to `sys.path` wholesale.
    """
    spec = importlib.util.spec_from_file_location(f"harness_{name}", E2E_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_lifecycle_module = _load("phase_e_lifecycle")

Lifecycle = _lifecycle_module.Lifecycle
DEFAULT_RUNTIME_BIN = _lifecycle_module.DEFAULT_RUNTIME_BIN
default_runtime_bin = _lifecycle_module.default_runtime_bin

#: The raw E1 arm: the control that shows a proxy variable in a parent environment can reach a tool
#: at all. Read as source/AST rather than imported -- it imports `qoder_agent_sdk`, which is present
#: for the Bridge interpreter only.
RAW_PROBE = E2E_DIR / "f3_e1_raw_probe.py"

#: The acceptance harness itself, for the structural pins below. Read as source: it imports the
#: Bridge and phase-E helpers, and these pins are about its shape rather than its runtime behaviour.
ACCEPTANCE = E2E_DIR / "run_phase_f_acceptance.py"


def _acceptance_source() -> str:
    return ACCEPTANCE.read_text(encoding="utf-8")


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _product_overlay(base: dict[str, str]) -> dict[str, str | None]:
    """`build_runtime_environment_overlay` for Qoder, imported from the Bridge package itself.

    The same implementation the Qoder adapter applies, so the shape pinned here is the shape the
    product actually hands the SDK.
    """
    bridge_src = REPO_ROOT / "agent_bridge" / "src"
    if not bridge_src.is_dir():
        pytest.skip("the Bridge package source is not present in this checkout")
    if str(bridge_src) not in sys.path:
        sys.path.insert(0, str(bridge_src))
    from serverfs_agent_bridge.runtime_proxy import build_runtime_environment_overlay

    return build_runtime_environment_overlay(base, runtime="qoder")


def _sdk_default_env() -> dict[str, Any] | None:
    """`QoderAgentOptions().env` under the Bridge interpreter, or None when it is not installed.

    A subprocess for the same reason the probe is one: `qoder-agent-sdk` is installed for the Bridge
    interpreter only, and the default the raw arm relies on has to be read from that interpreter.
    """
    bridge_python = Path(_lifecycle_module.BRIDGE_PYTHON)
    if not bridge_python.exists():
        return None
    source = (
        "import json\n"
        "from qoder_agent_sdk import QoderAgentOptions\n"
        "print(json.dumps(QoderAgentOptions().env))\n"
    )
    completed = subprocess.run(
        [str(bridge_python), "-c", source],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return None
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _lifecycle(tmp_path: Path, **kwargs: Any) -> Lifecycle:
    return Lifecycle(
        tmp_path,
        env_file=REPO_ROOT / ".env",
        codex_home=REPO_ROOT / "agent_bridge" / ".venv",
        **kwargs,
    )


class TestTheRenderedExecutable:
    def test_qoder_renders_the_product_default_not_the_runtime_name(self, tmp_path: Path) -> None:
        """`qoder_bin` must be `qodercli`, which is `QoderSettings.qoder_bin`'s default."""
        text = _lifecycle(tmp_path, runtime="qoder").config_path().read_text(encoding="utf-8")
        assert 'qoder_bin = "qodercli"' in text
        # And explicitly not the dispatcher shim the runtime name would have produced.
        assert 'qoder_bin = "qoder"' not in text

    @pytest.mark.parametrize(
        ("runtime", "expected"),
        [("codex", "codex"), ("claude", "claude"), ("qoder", "qodercli")],
    )
    def test_every_runtime_renders_its_product_default(
        self, tmp_path: Path, runtime: str, expected: str
    ) -> None:
        lifecycle = _lifecycle(tmp_path / runtime, runtime=runtime)
        text = lifecycle.config_path().read_text(encoding="utf-8")
        assert f'{runtime}_bin = "{expected}"' in text

    def test_the_mapping_matches_the_product_defaults(self) -> None:
        """The harness's mapping is not a preference; it mirrors the product's own defaults.

        Read from the Bridge package rather than hardcoded a second time, so a product default that
        changes fails here instead of silently diverging in every future acceptance run.
        """
        bridge_src = REPO_ROOT / "agent_bridge" / "src"
        if not bridge_src.is_dir():
            pytest.skip("the Bridge package source is not present in this checkout")
        sys.path.insert(0, str(bridge_src))
        try:
            from serverfs_agent_bridge.config import (
                ClaudeSettings,
                CodexSettings,
                QoderSettings,
            )
        except ImportError:  # pragma: no cover - the package always ships these
            pytest.skip("Bridge config module not importable")
        assert DEFAULT_RUNTIME_BIN["qoder"] == QoderSettings().qoder_bin
        assert DEFAULT_RUNTIME_BIN["codex"] == CodexSettings().codex_bin
        assert DEFAULT_RUNTIME_BIN["claude"] == ClaudeSettings().claude_bin

    def test_an_override_is_used_verbatim(self, tmp_path: Path) -> None:
        """A diagnostic may measure a non-default path, but only by naming it explicitly."""
        text = (
            _lifecycle(tmp_path, runtime="qoder", runtime_bin="qoder.cmd")
            .config_path()
            .read_text(encoding="utf-8")
        )
        assert 'qoder_bin = "qoder.cmd"' in text

    def test_an_unknown_runtime_falls_back_to_its_own_name(self) -> None:
        assert default_runtime_bin("nonexistent") == "nonexistent"


class TestSentinelsMustExistBeforeSpawn:
    def test_extra_child_env_is_present_in_the_launcher_environment(self, tmp_path: Path) -> None:
        """The fix for the vacuous E1: sentinels are in `child_env()`, i.e. before `Popen`."""
        lifecycle = _lifecycle(
            tmp_path,
            runtime="qoder",
            extra_child_env={"HTTPS_PROXY": "http://sentinel.invalid:1"},
        )
        env = lifecycle.child_env()
        assert env["HTTPS_PROXY"] == "http://sentinel.invalid:1"

    def test_extra_child_env_cannot_be_shadowed_by_the_pollution_markers(
        self, tmp_path: Path
    ) -> None:
        """Applied last, so a sentinel is never overwritten by a marker of the same name.

        `child_env()` sets the pollution markers before this, so ordering is the whole contract: an
        `extra_child_env` entry that lost would produce a chain that looks configured and is not.
        """
        lifecycle = _lifecycle(
            tmp_path,
            runtime="qoder",
            extra_child_env={"HTTP_PROXY": "http://sentinel.invalid:2"},
        )
        assert lifecycle.child_env()["HTTP_PROXY"] == "http://sentinel.invalid:2"

    def test_no_extra_child_env_leaves_the_environment_as_it_was(self, tmp_path: Path) -> None:
        """The default path is unchanged, so Phase E's Codex chain stays byte-for-byte identical."""
        lifecycle = _lifecycle(tmp_path, runtime="codex")
        assert "HTTPS_PROXY" not in lifecycle.child_env() or (
            lifecycle.child_env()["HTTPS_PROXY"] == "http://generic-marker:8080"
        )


class TestTheE1ArmCannotMeasureWithPostLaunchSentinels:
    def test_the_old_e1_entry_point_refuses_rather_than_reports(self) -> None:
        """`_e1_non_vacuity` must raise, not return a vacuous `non_vacuous=False`.

        The failure it used to report was indistinguishable from a genuine refusal, which is the
        shape that stops every real run while appearing to enforce the rule. A deprecated arm that
        refuses is safe; one that quietly measures nothing is not.
        """
        acceptance = _load("run_phase_f_acceptance")
        import asyncio

        with pytest.raises(RuntimeError, match="pre-spawn sentinel"):
            asyncio.run(
                acceptance._e1_non_vacuity(None, None, "http://endpoint.invalid")  # type: ignore[arg-type]
            )


class TestTheRawE1ArmCarriesNoServerFSPolicy:
    """E1's meaning is "no ServerFS scrub in the path"; that has to be enforced, not assumed.

    The defect pinned here: the raw probe built its options with the product's own
    ``build_runtime_environment_overlay``. The caller installs the sentinels before the probe's
    subprocess is spawned, so the probe deleted them itself and the arm reported
    ``non_vacuous=false`` -- the exact vacuity it exists to rule out, in the direction that reads as
    a safe refusal.
    """

    def test_the_probe_source_does_not_reference_the_product_overlay(self) -> None:
        source = RAW_PROBE.read_text(encoding="utf-8")
        assert "build_runtime_environment_overlay" not in source
        # The whole Bridge namespace, so a differently-named import cannot slip back in.
        assert "serverfs_agent_bridge" not in source

    def test_the_probe_builds_its_options_without_an_env_overlay(self) -> None:
        """No `env` keyword, so the SDK default (`{}`) applies and inheritance stays natural."""
        tree = ast.parse(RAW_PROBE.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _called_name(node) == "QoderAgentOptions"
        ]
        assert calls, "the raw probe must construct QoderAgentOptions"
        for call in calls:
            assert "env" not in {keyword.arg for keyword in call.keywords}, (
                "raw E1 must pass no env: any overlay here is a scrub, and a scrub in the raw arm "
                "makes the arm vacuous"
            )


class TestTheTwoArmsConfigureDifferentEnvironments:
    """The contract pair, pinned deterministically.

        E1 raw     -> QoderAgentOptions.env == {}
        E2 product -> deletion overlay, every value None

    Neither assertion needs a provider. What is pinned is the configuration each arm hands the SDK,
    which is precisely the difference that makes E2's absence meaningful.
    """

    def test_the_sdk_default_environment_is_empty(self) -> None:
        """What the raw arm inherits: it passes no `env`, so the SDK default applies."""
        default = _sdk_default_env()
        if default is None:
            pytest.skip("qoder-agent-sdk is not installed for the Bridge interpreter")
        assert default == {}

    def test_the_product_overlay_is_deletion_only(self) -> None:
        overlay = _product_overlay({"HTTPS_PROXY": "http://sentinel.invalid:1", "PATH": "/usr/bin"})
        assert overlay, "a populated base environment must produce a non-empty overlay"
        assert set(overlay.values()) == {None}

    def test_the_product_overlay_would_remove_every_e1_sentinel(self) -> None:
        """Why the raw arm may not use it: it deletes exactly the names E1 installs."""
        sentinels = (
            "HTTPS_PROXY",
            "ALL_PROXY",
            "CODEBUDDY_SERVICE_PROXY_URL",
            "SERVERFS_AGENT_PROXY_URL",
        )
        base = {**{name: "http://sentinel.invalid:1" for name in sentinels}, "PATH": "/usr/bin"}
        overlay = _product_overlay(base)
        removed = {name for name, value in overlay.items() if value is None}
        assert set(sentinels) <= removed

    def test_the_product_overlay_leaves_unrelated_names_alone(self) -> None:
        """Deletion-only, and only of policy names: `PATH` must not appear in the overlay at all."""
        overlay = _product_overlay({"PATH": "/usr/bin", "HTTPS_PROXY": "http://sentinel.invalid:1"})
        assert "PATH" not in overlay


#: Kept in sync with the acceptance's own list by `test_the_gate_list_matches_the_contract`, so a
#: gate added there without a verdict test fails here instead of being silently uncovered.
_GATE_NAMES: tuple[str, ...] = (
    "e1_ownership",
    "e1_model_gate",
    "e1",
    "e1_product",
    "e1_chain_gone",
    "ownership",
    "model_gate",
    "probe",
    "model_discovery",
    "e2",
    "workspace_write",
    "continuation",
    "approval",
    "question",
    "model_override",
    "cancellation",
    "cleanup",
)


def _all_pass_results() -> dict[str, Any]:
    """A results dict where every expected gate states an explicit pass (the positive control)."""
    acceptance = _load("run_phase_f_acceptance")
    return {
        name: acceptance.gate_record(True, status="succeeded") for name in acceptance.EXPECTED_GATES
    }


class TestTheAcceptanceVerdictCanFail:
    """A verdict that cannot fail is the shape this phase keeps misreading.

    The first real F3 run aborted with `TimeoutError` at the question gate: `question`,
    `model_override` and `cancellation` never ran, and the run still printed `F3_PASS`. The second
    version then added "a non-empty dict is a pass", which let a gate whose own evidence said
    `all_absent: false` still count -- one level down, the same false-PASS direction. The contract
    is now exactly one field per gate, and the ways it must be able to fail are pinned here.
    """

    def test_the_gate_list_matches_the_contract(self) -> None:
        acceptance = _load("run_phase_f_acceptance")
        assert tuple(_GATE_NAMES) == tuple(acceptance.EXPECTED_GATES)

    def test_a_complete_run_passes(self) -> None:
        acceptance = _load("run_phase_f_acceptance")
        assert acceptance._verdict(_all_pass_results())["answer"] == "F3_PASS"

    def test_an_aborted_run_is_not_a_pass(self) -> None:
        acceptance = _load("run_phase_f_acceptance")
        incomplete = ("question", "model_override", "cancellation")
        results = {k: v for k, v in _all_pass_results().items() if k not in incomplete}
        results["run_failed"] = {
            "error_class": "TimeoutError",
            "detail": "task agt_x stayed 'waiting_for_question' for 900s",
        }
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["aborted"] == ["run_failed"]
        assert set(verdict["not_run"]) == set(incomplete)

    def test_a_missing_gate_alone_is_not_a_pass(self) -> None:
        """Absence is not success: an unreached gate must fail the verdict on its own."""
        acceptance = _load("run_phase_f_acceptance")
        results = _all_pass_results()
        del results["cancellation"]
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["not_run"] == ["cancellation"]

    def test_an_empty_gate_dict_is_not_a_pass(self) -> None:
        acceptance = _load("run_phase_f_acceptance")
        results = _all_pass_results()
        results["workspace_write"] = {}
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["failed"] == ["workspace_write"]

    def test_a_non_empty_gate_without_an_explicit_pass_is_not_a_pass(self) -> None:
        """The exact shape the second version let through, one level down.

        `{"status": "succeeded", "all_absent": false}` was counted as a pass because the dict was
        non-empty. It states nothing about the outcome; only an explicit `passed` does. This is the
        assertion the earlier, mis-named `..._nested_false_...` test claimed to make while actually
        feeding the verdict an empty dict.
        """
        acceptance = _load("run_phase_f_acceptance")
        results = _all_pass_results()
        results["e2"] = {"status": "succeeded", "all_absent": False}
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["failed"] == ["e2"]

    def test_an_explicitly_failed_gate_is_not_a_pass(self) -> None:
        acceptance = _load("run_phase_f_acceptance")
        results = _all_pass_results()
        results["cancellation"] = acceptance.gate_record(
            False, final_status="succeeded", started_artifact_seen=False
        )
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["failed"] == ["cancellation"]

    @pytest.mark.parametrize("gate", _GATE_NAMES)
    def test_every_expected_gate_can_fail_the_verdict(self, gate: str) -> None:
        """Per gate, because a gate the contract does not cover is a gate that cannot fail."""
        acceptance = _load("run_phase_f_acceptance")
        results = _all_pass_results()
        results[gate] = acceptance.gate_record(False)
        verdict = acceptance._verdict(results)
        assert verdict["answer"] == "F3_PARTIAL"
        assert verdict["failed"] == [gate]


class TestOnlyOneChainExistsAtATime:
    """The Agent endpoint is derived from the user SID alone, so two chains share one pipe.

    Measured: the second chain got no Bridge of its own, its client attached to the first chain's
    Bridge, and its task, artifact and store row all landed in the first chain -- while every tool
    call succeeded. The E1 pair therefore re-measured the ordinary chain instead of the sentinel
    chain, and nothing failed. Isolation has to be temporal, and ownership has to be asserted.
    """

    def test_the_ownership_precondition_is_called_for_both_chains(self) -> None:
        source = _acceptance_source()
        assert source.count("require_own_bridge(") >= 2, (
            "each launched chain must assert it owns its Bridge before any tool result is trusted"
        )

    def test_the_first_chain_is_stopped_before_the_second_is_constructed(self) -> None:
        source = _acceptance_source()
        stopped = source.index("e1_lifecycle.stop()")
        second_chain = source.index('prefix="phase-f3-acceptance-"')
        assert stopped < second_chain, (
            "the ordinary chain must not be launched while the sentinel chain still owns the "
            "user-scoped endpoint"
        )

    def test_a_lifecycle_without_its_own_bridge_is_refused(self) -> None:
        class NoBridge:
            process = None

            def bridge_pids(self) -> list[int]:
                return []

            def supervisor_pids(self) -> list[int]:
                return []

        with pytest.raises(_lifecycle_module.BridgeOwnershipError, match="FAIL HARNESS"):
            _lifecycle_module.require_own_bridge(NoBridge(), timeout=0.05)

    def test_a_lifecycle_that_owns_its_bridge_is_accepted(self) -> None:
        class Alive:
            def poll(self) -> None:
                return None

        class OwnBridge:
            process = Alive()

            def bridge_pids(self) -> list[int]:
                return [4242]

            def supervisor_pids(self) -> list[int]:
                return [4241]

        assert _lifecycle_module.require_own_bridge(OwnBridge(), timeout=0.05) == {
            "own_bridge_pid_count": 1,
            "own_supervisor_present": True,
            "launcher_alive": True,
        }


class TestTheApprovedFreeModelIsAlwaysNamed:
    """Every real-provider turn names `qfmodel`, except the one arm whose omission *is* the test."""

    def test_the_raw_probe_passes_an_explicit_model(self) -> None:
        tree = ast.parse(RAW_PROBE.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _called_name(node) == "QoderAgentOptions"
        ]
        assert calls
        for call in calls:
            assert "model" in {keyword.arg for keyword in call.keywords}, (
                "the raw arm must name the model; a host default that happens to be free today is "
                "not an acceptance guarantee"
            )

    def test_the_raw_probe_frame_carries_the_approved_model(self) -> None:
        assert '"model": FLASH_MODEL_ID' in _acceptance_source()

    def test_no_probe_submit_omits_the_model(self) -> None:
        source = _acceptance_source()
        assert source.count("ENV_PROBE_PROMPT, runtime=RUNTIME, model=FLASH_MODEL_ID") >= 2
        assert "ENV_PROBE_PROMPT, runtime=RUNTIME)" not in source, (
            "an env-probe submit that omits `model` may silently use a paid host default"
        )

    def test_the_omitted_model_arm_is_guarded_by_the_native_default(self) -> None:
        source = _acceptance_source()
        guard = source.index("native_default = qoder_native_default_model()")
        omitted = source.index("qoder-default-ok")
        assert guard < omitted, (
            "the omitted-model arm must prove the host default is the approved free model first"
        )

    def test_the_native_default_reader_only_reads(self) -> None:
        """A gate must never write to the operator's Qoder configuration to make itself pass."""
        source = _acceptance_source()
        start = source.index("def qoder_native_default_model()")
        end = source.index("async def live_model_gate")
        body = source[start:end]
        assert "write_text" not in body and "open(" not in body.replace("read_text", "")
