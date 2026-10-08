"""F3: real-provider acceptance for the Qoder runtime, through the public MCP surface only.

Runs the maintainer's order, and stops at the first real STOP condition rather than substituting:

    live model gate -> probe -> model discovery -> E1 -> E2 -> new session + native ids
    -> workspace-write -> continuation -> approval -> AskUserQuestion -> model override
    -> cancellation -> cleanup

Two of those gates exist because F0 could not measure them. **E1** proves the tool probe is not
vacuous by putting the *real* Agent proxy into the environment and showing the tool can see it.
**E2** runs the same probe through the product chain and requires every name to be gone. F0
established that `set_proxy()` governs provider egress; neither of these re-derives that, and
neither needs a test observer -- the dedicated endpoint is the real thing, which is the whole point
of the trust boundary.

Nothing prints a proxy value, a hostname, a port, a model account, or a native identifier. Presence
booleans and booleans about identity are what get recorded.

The live model gate is re-read at the start of every run. `qfmodel` is the only model used; if it is
absent, disabled, or no longer free, the run stops rather than quietly paying for `Qwen3.8-Max`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from phase_e_acceptance import (  # noqa: E402
    TERMINAL,
    McpStdioClient,
    ToolError,
    compare_native_ids,
    native_ids,
    task_in_store,
)
from phase_e_lifecycle import (  # noqa: E402
    BRIDGE_PYTHON,
    HarnessPreflightError,
    Lifecycle,
    load_env_file,
    require_file_stderr,
    require_own_bridge,
    require_preflight,
)

#: The only model this acceptance will use. The free offering that made it affordable ended in
#: September, so the gate below re-checks the live metadata rather than assuming either answer.
FLASH_MODEL_ID = "qfmodel"
RUNTIME = "qoder"

#: Names the scrub must remove. E1 shows them arriving; E2 shows them gone. Values are the real
#: endpoint and are never printed -- only whether a name was present.
SCRUB_NAMES = (
    "HTTPS_PROXY",
    "ALL_PROXY",
    "CODEBUDDY_SERVICE_PROXY_URL",
    "SERVERFS_AGENT_PROXY_URL",
)

PROBE_ARTIFACT = "phase-f3-toolenv.json"

#: A tool-using turn is required for anything network- or environment-related. F0 measured that a
#: trivial "reply with one word" prompt completes over a path the proxy never touches.
ENV_PROBE_PROMPT = (
    "Run this exact shell command with the Bash tool and do nothing else: "
    f"python -c \"import json,os;open('{PROBE_ARTIFACT}','w').write(json.dumps("
    "{k: (k in os.environ) for k in " + repr(list(SCRUB_NAMES)) + '}))"'
)


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def agent_endpoint() -> str:
    """The dedicated endpoint, from wherever Phase E left it.

    Read from the private env file first and the process environment second, because the acceptance
    launcher's home for it is that file. Raises rather than returning empty: E2 is meaningless
    without it, and substituting a stand-in is exactly what the maintainer ruled out.
    """
    from serverfs_mcp.agent_proxy import AGENT_PROXY_URL_ENV

    file_env = load_env_file(REPO_ROOT / ".env")
    value = file_env.get(AGENT_PROXY_URL_ENV, "") or os.environ.get(AGENT_PROXY_URL_ENV, "")
    value = value.strip()
    if not value:
        raise RuntimeError("the dedicated Agent proxy endpoint is not configured")
    return value


def qoder_native_default_model() -> str | None:
    """The model the host's Qoder CLI would use when a task omits `model`. Read-only.

    Read from the operator's own settings file and nothing else: this is a precondition check for
    the one arm that deliberately omits a model, and the acceptance must never write to that file to
    make a gate pass.
    """
    path = Path.home() / ".qoder" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("name")
        return str(name) if isinstance(name, str) and name else None
    return str(model) if isinstance(model, str) and model else None


async def live_model_gate(client: McpStdioClient) -> dict[str, Any]:
    """Re-assert the free-model gate against the live catalog, before any task is submitted."""
    catalog = client.call("list_agent_models", {"runtime": RUNTIME})
    entries = catalog.get("models") or []
    # An empty catalog has two very different causes, and conflating them produces a fictional
    # diagnosis: the runtime may be unavailable (no CLI, not signed in), or discovery may have
    # failed while the runtime itself is fine. `status` and `detail` are the Bridge's own statement
    # of which, so an empty list is never reported as "the model is gone" without them.
    if not entries:
        return gate_record(
            False,
            gate="STOP",
            clause="CATALOG_EMPTY",
            runtime_status=catalog.get("status"),
            detail=str(catalog.get("detail") or "")[:120],
            catalog_count=0,
        )
    match = next((m for m in entries if (m.get("id") or m.get("modelId")) == FLASH_MODEL_ID), None)
    if match is None:
        return gate_record(False, gate="STOP", clause="ABSENT", catalog_count=len(entries))
    # The Bridge normalises the catalog, so the public shape is snake_case: `enabled`, `is_free`,
    # `price_factor`. It also omits `is_free` / `price_factor` entirely unless the provider supplied
    # a bool / a number, so a missing key means "the provider did not say" rather than "false",
    # and a gate reading the raw camelCase names saw three absent fields and stopped a
    # healthy model.
    enabled = match.get("enabled", match.get("isEnabled"))
    free = match.get("is_free", match.get("isFree"))
    price = match.get("price_factor", match.get("priceFactor"))
    clause = (
        "FREE_AND_ENABLED"
        if enabled is True and free is True and not (isinstance(price, (int, float)) and price > 0)
        else ("NOT_ENABLED" if enabled is not True else "NOT_FREE")
    )
    return gate_record(
        clause == "FREE_AND_ENABLED",
        gate="GO" if clause == "FREE_AND_ENABLED" else "STOP",
        clause=clause,
        catalog_count=len(entries),
        enabled=enabled,
        is_free=free,
        price_factor=price,
    )


def read_probe(workdir: Path) -> dict[str, bool] | None:
    path = workdir / PROBE_ARTIFACT
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def wait_for_probe(
    workdir: Path, *, timeout: float = 15.0, poll: float = 0.2
) -> tuple[dict[str, bool] | None, float | None]:
    """Wait, boundedly, for the tool's probe artifact to exist *and* parse. Returns (probe, delay).

    A task can report a terminal status before its last file operation is complete for another
    process, so a single immediate read races it. Observed across five runs: the paired E1 arm
    reported `probe_present=false` in both full runs and `true` in every targeted run -- and the
    only structural difference is that the targeted path's first read happens after an extra public
    read and a directory scan, i.e. a fraction of a second later. Existence and complete contents
    are two different readings, so this waits on the **parsed** artifact, which is what the gate
    actually consumes.

    Bounded on purpose: "readable after N seconds" and "never readable" are different findings, and
    only the second fails the gate. Without it, the strengthened gate would have inherited the race
    as intermittent failure for a reason unrelated to its own claim.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        probe = read_probe(workdir)
        if probe is not None:
            return probe, round(time.monotonic() - start, 2)
        time.sleep(poll)
    return None, None


#: The gates an F3 run must close. "Did not run" is a first-class outcome, not an absence: the
#: first real run aborted at the question gate, every later gate was never submitted, and the
#: verdict still printed `F3_PASS` -- because a missing key is neither `None` nor `False`.
#:
#: Every gate reports an explicit ``passed`` boolean and the verdict reads nothing else. Inferring
#: the outcome from whichever booleans happen to sit inside a payload is what let a non-empty gate
#: dict carrying ``all_absent: false`` still count as a pass.
EXPECTED_GATES: tuple[str, ...] = (
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


def gate_record(passed: bool, **evidence: Any) -> dict[str, Any]:
    """One gate's outcome. ``passed`` is the only field the verdict reads.

    The gate states its own verdict rather than leaving the verdict to infer one -- the payloads
    carry booleans that are legitimately ``None`` or ``False`` without meaning failure, so an
    inferred rule is either too strict or, as measured here, too lax.
    """
    return {"passed": bool(passed), **evidence}


def _verdict(results: dict[str, Any]) -> dict[str, Any]:
    """The run's outcome, computed so that nothing but an explicit pass can read as PASS.

    The contract is deliberately narrow and auditable:

        every expected gate is present, each reports ``passed is True``,
        and no run failure or tool error was recorded.

    Two earlier versions could not fail correctly. The first inspected only top-level
    ``None``/``False``, so an aborted run -- recorded as a non-empty string, with the unreached
    gates simply absent -- printed ``F3_PASS``. The second added "a non-empty dict is a pass",
    which let a gate whose own evidence said ``all_absent: false`` still count: the same false-PASS
    direction, one level down. Hence one field, stated by each gate, and a gate that omits it fails.
    """
    not_run = [name for name in EXPECTED_GATES if name not in results]
    aborted = [name for name in ("run_failed", "tool_error") if name in results]
    failed = [
        name
        for name in EXPECTED_GATES
        if name in results
        if not (isinstance(results[name], dict) and results[name].get("passed") is True)
    ]
    ok = not failed and not not_run and not aborted
    return {
        "answer": "F3_PASS" if ok else "F3_PARTIAL",
        "failed": failed,
        "not_run": not_run,
        "aborted": aborted,
        "implication": (
            "Every gate in this run passed through the public MCP surface against the real "
            "provider."
            if ok
            else (
                "Not every expected gate completed with an explicit pass. Gates that did not run "
                "are listed under `not_run`; none is inferred from another."
            )
        ),
    }


async def main() -> int:
    endpoint = agent_endpoint()
    emit("setup", runtime=RUNTIME, endpoint_exposed=False, model=FLASH_MODEL_ID)

    require_preflight(REPO_ROOT / ".env", Path.home() / ".codex")

    results: dict[str, Any] = {}

    # ---- Phase 1: the sentinel chain, alone ------------------------------------------------
    # The Agent endpoint is derived from the user SID alone, so two chains in one user session share
    # one pipe and the second never gets a Bridge of its own -- measured, and it made the E1 pair
    # silently re-measure the ordinary chain (see `require_own_bridge`). One chain at a time is the
    # product's own contract, so the harness follows it rather than asking the product to change.
    e1_root = Path(tempfile.mkdtemp(prefix="phase-f3-e1-chain-"))
    e1_lifecycle = Lifecycle(
        e1_root,
        env_file=REPO_ROOT / ".env",
        codex_home=Path.home() / ".codex",
        use_proxy=True,
        read_only=False,
        runtime=RUNTIME,
        extra_child_env={name: endpoint for name in SCRUB_NAMES},
    )
    require_file_stderr(e1_lifecycle)
    e1_hard_failure: dict[str, Any] | None = None
    try:
        e1_lifecycle.launch()
        # Ownership before anything is trusted: a chain answering through another chain's Bridge
        # returns plausible results for the wrong subject.
        results["e1_ownership"] = gate_record(True, **require_own_bridge(e1_lifecycle))
        e1_client = McpStdioClient(e1_lifecycle)
        e1_client.initialize()
        # Re-price on this chain before any inference on it -- the raw arm is a real provider turn.
        e1_gate = await live_model_gate(e1_client)
        results["e1_model_gate"] = e1_gate
        emit("e1_model_gate", **e1_gate)
        if e1_gate["gate"] != "GO":
            emit("bridge_stderr_tail", tail=e1_lifecycle.stderr_text()[-1200:])
            emit(
                "verdict",
                answer="STOP_REAL_PROVIDER_ACCEPTANCE",
                phase="e1_chain",
                clause=e1_gate["clause"],
                implication=(
                    "The approved free model is gone, disabled or no longer free, so not even the "
                    "raw arm may run -- it is itself a real provider turn."
                ),
            )
            return 3

        # (a) raw inheritance, with no ServerFS adapter in the path: does a proxy variable in a
        # parent environment reach a tool at all? This is what the paired arm is measured against.
        e1_outcome = await _e1_raw_sdk_non_vacuity(endpoint, e1_lifecycle.child_env())
        results["e1"] = gate_record(e1_outcome.get("non_vacuous") is True, **e1_outcome)
        if not results["e1"]["passed"]:
            emit(
                "verdict",
                answer="STOP_E1_VACUOUS",
                implication=(
                    "The sentinels were installed before anything was spawned and the tool still "
                    "saw none of them. Do not read the scrub as working, and do not attribute this "
                    "to ServerFS: with the vacuity removed, the provider child may simply not "
                    "consult these names."
                ),
            )
            return 4
        # (b) the paired product arm, on this same sentinel-carrying chain.
        results["e1_product"] = await _run_gate(
            "e1_product", _e1_product_scrub(e1_client, e1_lifecycle)
        )
    except HarnessPreflightError as exc:
        e1_hard_failure = {"error_class": type(exc).__name__, "detail": str(exc)[:300]}
        emit("e1_harness_failure", **e1_hard_failure)
        results["run_failed"] = e1_hard_failure
    finally:
        try:
            e1_lifecycle.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            e1_lifecycle.kill()
        e1_left = e1_lifecycle.bridge_pids()
        # The second chain must not be launched over a Bridge the first one still owns.
        results["e1_chain_gone"] = gate_record(
            not e1_left, own_bridge_count_after_stop=len(e1_left)
        )
        shutil.rmtree(e1_root, ignore_errors=True)

    if e1_hard_failure is not None or results.get("e1_product", {}).get("passed") is not True:
        emit(
            "verdict",
            answer="STOP_E1_PAIR_NOT_CLOSED",
            e1_product_passed=results.get("e1_product", {}).get("passed"),
            implication=(
                "The paired E1 evidence did not close on a chain that provably owns its Bridge, so "
                "the scrub's causal half is unproven. Launching the ordinary chain would spend a "
                "run that cannot be accepted; the gate's own evidence is printed above."
            ),
        )
        return 5

    # ---- Phase 2: the ordinary chain -------------------------------------------------------
    tmp_root = Path(tempfile.mkdtemp(prefix="phase-f3-acceptance-"))
    lifecycle = Lifecycle(
        tmp_root,
        env_file=REPO_ROOT / ".env",
        codex_home=Path.home() / ".codex",
        use_proxy=True,
        read_only=False,
        runtime=RUNTIME,
    )
    # Formal acceptance refuses an undrained stderr pipe; Phase E established that as this harness's
    # own fault rather than a product defect, so it is a precondition rather than a diagnosis.
    require_file_stderr(lifecycle)

    client = McpStdioClient(lifecycle)
    try:
        lifecycle.launch()
        results["ownership"] = gate_record(True, **require_own_bridge(lifecycle))
        client.initialize()

        # --- gate first: nothing is submitted against a model we have not re-priced ---------
        gate = await live_model_gate(client)
        results["model_gate"] = gate
        emit("model_gate", **gate)
        if gate["gate"] != "GO":
            # The Bridge answers an empty catalog with a fixed `detail` and drops the exception, so
            # the chain's own log is the only place the cause exists. Reported as a tail: enough to
            # diagnose, not enough to become a transcript of the run.
            emit("bridge_stderr_tail", tail=lifecycle.stderr_text()[-1200:])
            emit(
                "verdict",
                answer="STOP_REAL_PROVIDER_ACCEPTANCE",
                phase="normal_chain",
                clause=gate["clause"],
                implication=(
                    "The free model is gone, disabled or no longer free. Do not substitute "
                    "Qwen3.8-Max: paying for a model this acceptance was not approved to use is "
                    "not a decision this run may make."
                ),
            )
            return 3

        # The real tool name is `list_agent_runtimes`; `agent_runtime_status` was invented, and the
        # failure surfaced as a ToolError rather than as a silently skipped gate.
        results["probe"] = await _gate(client, "probe", "list_agent_runtimes", {})
        results["model_discovery"] = gate_record(
            gate["clause"] == "FREE_AND_ENABLED" and gate["catalog_count"] > 0,
            catalog_count=gate["catalog_count"],
            model_present=True,
        )
        # E2: the ordinary deployment path -- same prompt, product scrub, no sentinels arranged.
        results["e2"] = await _run_gate("e2", _e2_product_scrub(client, lifecycle))

        results["workspace_write"] = await _run_gate(
            "workspace_write", _workspace_write(client, lifecycle)
        )
        results["continuation"] = await _run_gate(
            "continuation", _continuation(client, lifecycle, results)
        )
        results["approval"] = await _run_gate("approval", _approval(client))
        results["question"] = await _run_gate("question", _question(client, lifecycle))
        results["model_override"] = await _run_gate(
            "model_override", _model_override(client, results)
        )
        results["cancellation"] = await _run_gate("cancellation", _cancellation(client, lifecycle))
    except ToolError as exc:
        emit("tool_error", detail=str(exc)[:200])
        results["tool_error"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        # The message is kept, not just the class. The first real run aborted with a bare
        # `TimeoutError` whose text carried the status the task was stuck in -- the single fact
        # that identifies the cause -- and recording only the class threw it away, which is how a
        # diagnosable gate failure became an unexplained one.
        emit("run_failed", error_class=type(exc).__name__, detail=str(exc)[:300])
        results["run_failed"] = {"error_class": type(exc).__name__, "detail": str(exc)[:300]}
    finally:
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            lifecycle.kill()
        bridge_owned_gone = not lifecycle.codex_app_server_pids()
        try:
            qoder_pids = lifecycle.bridge_pids()
        except Exception:  # noqa: BLE001
            qoder_pids = []
        bridge_stopped = not qoder_pids
        # Cleanup is a gate too: "0 residual provider trees" is an F3 exit criterion, so it states
        # its own `passed` instead of leaving two loose booleans for the verdict to ignore.
        results["cleanup"] = gate_record(
            bridge_owned_gone and bridge_stopped,
            bridge_owned_gone=bridge_owned_gone,
            bridge_stopped=bridge_stopped,
        )
        shutil.rmtree(tmp_root, ignore_errors=True)
        emit("cleanup", **results["cleanup"])

    emit("results", **{k: v for k, v in results.items() if k != "findings"})
    verdict = _verdict(results)
    emit("verdict", **verdict)
    return 0 if verdict["answer"] == "F3_PASS" else 5


async def _gate(client: McpStdioClient, label: str, tool: str, arguments: dict) -> dict[str, Any]:
    try:
        result = client.call(tool, arguments)
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        emit(label, ok=False, error_class=type(exc).__name__)
        return gate_record(False, error_class=type(exc).__name__)
    emit(label, ok=True)
    return gate_record(True, result=result)


async def _e1_non_vacuity(
    client: McpStdioClient, lifecycle: Lifecycle, endpoint: str
) -> dict[str, Any]:
    """DEPRECATED as a non-vacuity gate -- kept only so the old call site cannot silently pass.

    The sentinels used to be written into `os.environ` here, *after* `lifecycle.launch()`
    had already created the launcher/supervisor/Bridge tree. A child inherits its parent's
    environment as it was when `Popen` ran, so those names never reached the Bridge and
    never could: measured on this host, a variable added after `Popen` is invisible to the
    child (`CHILD_SEES <absent>`). E1 then reported `visible_to_tool=[]` for a reason that had
    nothing to do with the tool, which is the dangerous direction -- it looks like a safe
    refusal.

    Use `_e1_raw_sdk_non_vacuity` instead. This wrapper refuses rather than measures.
    """
    emit(
        "e1_rejected",
        answer="E1_SENTINELS_INJECTED_AFTER_LAUNCH",
        implication=(
            "This arm cannot prove non-vacuity: the sentinels were added to the harness "
            "environment after the Bridge process tree already existed. Call "
            "_e1_raw_sdk_non_vacuity, which installs them before the child is spawned."
        ),
    )
    raise RuntimeError("E1 must use the pre-spawn sentinel arm, not a post-launch os.environ write")


async def _e1_raw_sdk_non_vacuity(endpoint: str, chain_env: Mapping[str, str]) -> dict[str, Any]:
    """Show the provider child really can see a proxy variable when one is present.

    F0 could not measure this and deferred it to F3 as mandatory: the scrub is only
    proven by a pair, and the "removed" half means nothing without a "would have arrived" half.

    **The sentinels exist before the process is spawned**, because `chain_env` -- the
    environment the E1 chain was launched with -- already carries them into the launcher, the
    supervisor and the Bridge. Nothing here writes to `os.environ` after the fact: a child
    inherits the environment as it was at `Popen`, so a name added afterwards is invisible to
    it. Measured on this host, not assumed (`CHILD_SEES <absent>`).

    The probe runs the raw SDK rather than the product chain on purpose. E1 is about *raw
    inheritance* -- whether a name in a parent environment reaches a tool at all -- and routing it
    through ServerFS would test the scrub, which is E2's job, and collapse the two arms into one.

    It also runs as a **subprocess under the Bridge interpreter**: `qoder-agent-sdk` is installed in
    `agent_bridge/.venv` only, so an in-process import in this harness would fail outright, and a
    measurement taken under a different interpreter than production is not the same measurement.

    No ServerFS adapter is involved, so a failure here is a statement about the provider and the
    platform, never about the product's policy.
    """
    workspace = Path(tempfile.mkdtemp(prefix="phase-f3-e1-raw-"))
    probe_env = {**chain_env, "E1_PROBE_DIR": str(workspace)}
    sink = REPO_ROOT / "scratchpad" / "f3-e1-raw.stderr.log"
    sink.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sink.open("w", encoding="utf-8") as stderr_sink:
            process = subprocess.Popen(
                [str(BRIDGE_PYTHON), str(REPO_ROOT / "tests" / "e2e" / "f3_e1_raw_probe.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # A file, not a pipe: an undrained pipe truncated this harness's own evidence once
                # already, and the failure looked like a product stall.
                stderr=stderr_sink,
                env=probe_env,
                cwd=str(workspace),
            )
            stdout, _ = process.communicate(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "names": list(SCRUB_NAMES),
                        # Explicit, so this arm cannot silently fall back to whatever the host's
                        # Qoder settings name -- a default that is free today is not a guarantee.
                        "model": FLASH_MODEL_ID,
                    }
                ).encode("utf-8"),
                timeout=420,
            )
    except subprocess.TimeoutExpired:
        process.kill()
        emit("e1_raw_failed", error_class="SubprocessTimeout")
        return {"status": "failed", "error_class": "SubprocessTimeout", "non_vacuous": False}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    outcome: dict[str, Any] = {"non_vacuous": False, "visible_to_tool": []}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("stage") == "e1_raw":
            outcome = {k: v for k, v in record.items() if k != "stage"}
    emit(
        "e1_raw",
        returncode=process.returncode,
        sentinels_present_before_spawn=sorted(name for name in SCRUB_NAMES if name in chain_env),
        **outcome,
        stderr_tail=sink.read_text(encoding="utf-8", errors="replace")[-300:],
    )
    return outcome


async def _e1_product_scrub(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """The paired product arm of E1: the same sentinels, the same prompt, through the product chain.

    Raw E1 shows the sentinels reach a tool when nothing scrubs them. This arm shows that, on a
    launch whose environment demonstrably carried them, the product delivers **none** to a tool that
    actually ran. That is the causal half of the pair; E2 separately covers the ordinary deployment
    path, so the two are not duplicates.

    "The task succeeded and an approval happened" is **not** that claim. A turn that answered from
    its own reasoning, or whose tool never produced the artifact, satisfies it -- which is exactly
    how `probe_present=false` sat next to `passed=true`. The gate's predicate did not cover the fact
    its own docstring named, so the artifact and the empty visible-set are both required here.
    """
    probe_path = lifecycle.workdir / PROBE_ARTIFACT
    if probe_path.exists():
        probe_path.unlink()

    task_id = client.submit(ENV_PROBE_PROMPT, runtime=RUNTIME, model=FLASH_MODEL_ID)
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    events = client.event_types(task_id)
    requested = any("approval.requested" in n for n in events)
    resolved = any("approval.resolved" in n for n in events)
    answered = bool(client.observed_approvals.get(task_id))

    # -- one attributed diagnosis, taken before teardown, publishing no host path ----------
    # The provider's own "I wrote the file" text is not evidence -- it reports intent. These
    # readings separate the possible causes in a single run: the tool never ran; it ran with the
    # wrong cwd; the harness read the wrong place; or another chain served the request.
    expected_present = probe_path.exists()
    public_read_present = client.read_file(PROBE_ARTIFACT) is not None
    elsewhere = [p for p in lifecycle.tmp_path.rglob(PROBE_ARTIFACT) if p != probe_path]
    probe, artifact_delay_s = wait_for_probe(lifecycle.workdir)
    in_expected_store = task_in_store(lifecycle, task_id)
    artifact_in_expected_workdir = probe is not None
    emit(
        "e1_product_diagnosis",
        expected_probe_present=expected_present,
        public_read_probe_present=public_read_present,
        probe_found_elsewhere_under_lifecycle_root=bool(elsewhere),
        probe_elsewhere_count=len(elsewhere),
        artifact_parseable_after_s=artifact_delay_s,
        task_in_expected_store=in_expected_store,
        artifact_in_expected_workdir=artifact_in_expected_workdir,
        approval_requested=requested,
        approval_resolved=resolved,
        approval_answered=answered,
        task_status=status,
    )

    visible = sorted(n for n, present in (probe or {}).items() if present)
    emit(
        "e1_product_chain",
        status=status,
        probe_present=probe is not None,
        visible_to_tool=visible,
        approval_requested=requested,
        approval_resolved=resolved,
        event_types=events,
        command_started_means_request_only=True,
        final_response=(client.task(task_id).get("final_response") or "")[:300],
        chain_stderr_tail=lifecycle.stderr_text()[-900:],
    )
    passed = (
        status == "succeeded"
        and requested
        and resolved
        and answered
        and probe is not None
        and not visible
        # Ownership: a task served by another chain's Bridge lands in that chain's store and
        # workdir. Requiring both converts the measured cross-attachment from a silent
        # re-measurement of the ordinary chain into a failure.
        and in_expected_store is True
        and artifact_in_expected_workdir
    )
    return gate_record(
        passed,
        status=status,
        approval_requested=requested,
        approval_resolved=resolved,
        approval_answered=answered,
        probe_present=probe is not None,
        visible_to_tool=visible,
        task_in_expected_store=in_expected_store,
        artifact_in_expected_workdir=artifact_in_expected_workdir,
        task_id=task_id,
    )


async def _e2_product_scrub(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """The same probe, through the product chain, where the deletion overlay applies.

    This is the leg F0 could not close. It goes through `submit_agent_task` like any other task,
    so the environment the tool sees is whatever ServerFS decided it should be -- not something the
    harness arranged.
    """
    probe_path = lifecycle.workdir / PROBE_ARTIFACT
    if probe_path.exists():
        probe_path.unlink()

    task_id = client.submit(ENV_PROBE_PROMPT, runtime=RUNTIME, model=FLASH_MODEL_ID)
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    # Same read-after-terminal hazard as the paired E1 arm; bounded, with the delay recorded.
    probe, artifact_delay_s = wait_for_probe(lifecycle.workdir)
    visible = sorted(n for n, present in (probe or {}).items() if present)
    emit(
        "e2",
        status=status,
        probe_present=probe is not None,
        visible_to_tool=visible,
        all_absent=probe is not None and not visible,
        artifact_parseable_after_s=artifact_delay_s,
    )
    return gate_record(
        # The task must also have succeeded: an artifact that records "all absent" is not the same
        # as a turn that completed, and reading only the absence is how a partial turn passes.
        probe is not None and not visible and status == "succeeded",
        status=status,
        probe_present=probe is not None,
        all_absent=probe is not None and not visible,
        task_succeeded=status == "succeeded",
        artifact_parseable_after_s=artifact_delay_s,
    )


async def _workspace_write(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    task_id = client.submit(
        "Create phase-f3-qoder.txt containing exactly:\n\nqoder-workspace-write\n\n"
        "Do not modify any other file.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    artifact = lifecycle.workdir / "phase-f3-qoder.txt"
    exact = (
        artifact.exists()
        and artifact.read_text(encoding="utf-8").strip() == "qoder-workspace-write"
    )
    # Native identity lives in the TaskStore, not the public projection: `get_task` pops both ids
    # deliberately. Reading them from `client.task()` -- as the earlier version did -- could never
    # be true, so that check was vacuous. Reuses the Phase E reader rather than a second copy.
    store = native_ids(client, lifecycle, task_id)
    public = client.task(task_id)
    public_absent = "native_session_id" not in public and "native_turn_id" not in public
    passed = (
        status == "succeeded"
        and exact
        and store["thread_id_present"]
        and store["turn_id_present"]
        and public_absent
    )
    emit(
        "workspace_write",
        status=status,
        artifact_exact=exact,
        thread_id_present=store["thread_id_present"],
        turn_id_present=store["turn_id_present"],
        store_found=store.get("store_found"),
        public_projection_hides_native_ids=public_absent,
    )
    return gate_record(
        passed,
        status=status,
        artifact_exact=exact,
        thread_id_present=store["thread_id_present"],
        turn_id_present=store["turn_id_present"],
        public_projection_hides_native_ids=public_absent,
        task_id=task_id,
    )


async def _continuation(
    client: McpStdioClient, lifecycle: Lifecycle, results: dict[str, Any]
) -> dict[str, Any]:
    source = results.get("workspace_write", {}).get("task_id")
    if not source:
        return gate_record(False, reason="no source task")
    task_id = client.submit(
        "Create phase-f3-continuation.txt containing exactly:\n\nqoder-continuation\n\n"
        "Do not modify any other file.",
        continue_from=source,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    # "The task succeeded" is not proof of continuation -- a fresh session with the same prompt
    # would satisfy it. The check is on identity: same native session, a different native turn,
    # both read from the TaskStore by the Phase E helper.
    identity = compare_native_ids(lifecycle, source, task_id)
    passed = status == "succeeded" and all(identity.values())
    emit("continuation", status=status, **identity)
    return gate_record(passed, status=status, task_id=task_id, **identity)


async def _approval(client: McpStdioClient) -> dict[str, Any]:
    task_id = client.submit(
        "Create phase-f3-approval.txt containing exactly:\n\napproved\n\n"
        "Do not modify any other file.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    events = client.event_types(task_id)
    requested = any("approval.requested" in n for n in events)
    resolved = any("approval.resolved" in n for n in events)
    # `wait_status` records every request id it actually answered, so this counts
    # provider-originated requests rather than restating the task's terminal status. The earlier
    # version derived "an approval was observed" from `status == "succeeded"`, which is a different
    # claim: a run whose approval never arrived would still have reported `approvals_observed=True`.
    answered = len(client.observed_approvals.get(task_id, [])) > 0
    passed = status == "succeeded" and requested and resolved and answered
    emit(
        "approval",
        status=status,
        approval_requested=requested,
        approval_resolved=resolved,
        requests_answered=answered,
    )
    return gate_record(
        passed,
        status=status,
        approval_requested=requested,
        approval_resolved=resolved,
        approvals_observed=answered,
    )


async def _run_gate(name: str, coro: Any) -> dict[str, Any]:
    """Run one gate; an exception inside it fails that gate, not the whole run.

    Previously any exception propagated to `main`'s handler, which aborted the chain: the first
    full run stopped at `question`, `model_override` and `cancellation` never executed, and their
    absence had to be reconstructed from `not_run`. A gate that raises is a gate that failed, and
    the remaining gates are still evidence worth collecting.
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - the failure class is the gate's result
        emit("gate_failed", gate=name, error_class=type(exc).__name__, detail=str(exc)[:250])
        return gate_record(False, error_class=type(exc).__name__, detail=str(exc)[:250])


def _select_option(questions: Any, label: str) -> tuple[str, str] | None:
    """The ``(question_id, option_id)`` for ``label``, from the provider's own normalized options.

    Measured, not assumed: the adapter normalizes each provider option to
    ``{"option_id": <label>, "label": <label>}`` under ``payload["questions"][n]``, with
    ``question_id`` of the form ``q0``. The service rejects an option id the provider never
    offered, so answering with an invented one would fail the round trip for the wrong reason.
    """
    if not isinstance(questions, list):
        return None
    for question in questions:
        if not isinstance(question, dict):
            continue
        for option in question.get("options", []):
            if isinstance(option, dict) and str(option.get("label", "")).lower() == label:
                return str(question.get("question_id")), str(option.get("option_id"))
    return None


async def _question(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    artifact = lifecycle.workdir / "phase-f3-question.txt"
    if artifact.exists():
        artifact.unlink()

    task_id = client.submit(
        "Before making any workspace change, ask me one question using your user-input mechanism "
        "and wait for my answer.\n\nAsk: Which marker should I write?\n\nOffer exactly two "
        "choices: alpha, beta.\n\nAfter I answer, create phase-f3-question.txt containing "
        "exactly the selected marker. Do not modify any other file.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    # `wait_status` answers approvals and has no branch for a question, so the question leg waits on
    # the status it actually cares about first. Waiting on `wait_status` here is exactly how the
    # first full run sat for 900 s and then reported a bare TimeoutError.
    client.wait_for(task_id, {"waiting_for_question"}, timeout=420)
    request_id, nested = client.pending_request(task_id)
    kind = nested.get("kind")
    payload = nested.get("payload")
    questions = payload.get("questions") if isinstance(payload, dict) else None
    selection = _select_option(questions, "alpha")
    provider_option_ids = [
        [o.get("option_id") for o in q.get("options", []) if isinstance(o, dict)]
        for q in (questions or [])
        if isinstance(q, dict)
    ]
    if kind != "question" or selection is None:
        emit(
            "question",
            status="invalid_pending_request",
            pending_kind=kind,
            selection_found=selection is not None,
            provider_option_ids=provider_option_ids,
        )
        return gate_record(
            False,
            status="invalid_pending_request",
            pending_kind=kind,
            selection_found=selection is not None,
        )

    question_id, option_id = selection
    client.call(
        "answer_agent_question",
        {
            "task_id": task_id,
            "request_id": request_id,
            "answers": [{"question_id": question_id, "selected_option_ids": [option_id]}],
        },
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    events = client.event_types(task_id)
    content = artifact.read_text(encoding="utf-8").strip() if artifact.exists() else None
    requested = any("question.requested" in n for n in events)
    answered = any("question.answered" in n for n in events)
    passed = status == "succeeded" and requested and answered and content == "alpha"
    emit(
        "question",
        status=status,
        question_requested=requested,
        question_answered=answered,
        artifact_is_alpha=content == "alpha",
        pending_kind=kind,
        provider_option_ids=provider_option_ids,
    )
    return gate_record(
        passed,
        status=status,
        question_requested=requested,
        question_answered=answered,
        artifact_is_alpha=content == "alpha",
    )


async def _model_override(client: McpStdioClient, results: dict[str, Any]) -> dict[str, Any]:
    task_id = client.submit(
        "Reply with exactly:\n\nqoder-model-ok\n\nDo not use tools.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=False)
    record = client.task(task_id)
    requested = record.get("requested_model")

    # The second arm deliberately omits `model`: that omission is the property under test, because
    # the gate exists to show a request-scoped override does not leak into the next task. It is the
    # only real-provider turn allowed to omit one, so the host default must be *proved* to be the
    # approved free model first. Nothing is written to the operator's Qoder configuration.
    native_default = qoder_native_default_model()
    if native_default != FLASH_MODEL_ID:
        emit(
            "model_override",
            override_status=status,
            override_recorded=requested == FLASH_MODEL_ID,
            omitted_arm_run=False,
            native_default_is_approved_free_model=False,
            native_default_known=native_default is not None,
        )
        return gate_record(
            False,
            override_status=status,
            override_recorded=requested == FLASH_MODEL_ID,
            omitted_arm_run=False,
            native_default_is_approved_free_model=False,
            detail=(
                "STOP model_override real smoke: the host's Qoder default is not the approved "
                "free model, so the omitted-model arm could incur an unapproved charge"
            ),
        )

    default_task = client.submit(
        "Reply with exactly:\n\nqoder-default-ok\n\nDo not use tools.", runtime=RUNTIME
    )
    default_status = client.wait_status(default_task, timeout=900, allow_approval=False)
    default_record = client.task(default_task)
    inherited = default_record.get("requested_model")
    emit(
        "model_override",
        override_status=status,
        override_recorded=requested == FLASH_MODEL_ID,
        default_status=default_status,
        default_inherited=inherited,
        omitted_arm_run=True,
        native_default_is_approved_free_model=True,
    )
    passed = (
        status == "succeeded"
        and requested == FLASH_MODEL_ID
        and default_status == "succeeded"
        and inherited is None
    )
    return gate_record(
        passed,
        override_status=status,
        override_recorded=requested == FLASH_MODEL_ID,
        default_status=default_status,
        default_inherited=inherited,
        nothing_inherited=inherited is None,
        omitted_arm_run=True,
        native_default_is_approved_free_model=True,
    )


CANCEL_STARTED = "phase-f3-cancel-started.txt"
CANCEL_COMPLETION = "phase-f3-cancel-should-not-exist.txt"
AFTER_CANCEL = "phase-f3-after-cancel.txt"


async def _cancellation(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """Cancel a task that is genuinely mid-execution, proven by a file and not by an event name.

    The earlier detector waited for ``item.started`` / ``turn.started``. The Qoder adapter emits
    neither -- it emits ``command.started`` / ``tool.started`` / ``file_change.started`` -- so
    ``saw_activity`` could never become true, and `command.started` is not a substitute: it is
    emitted from a provider ``ToolUseBlock``, which means the model *requested* Bash, not that a
    process started.

    So the command writes a start marker *first*, and the gate waits for that marker to exist on
    disk. Only a real OS process can produce it. The command then sleeps and would write a
    completion marker that must never appear.
    """
    started = lifecycle.workdir / CANCEL_STARTED
    completion = lifecycle.workdir / CANCEL_COMPLETION
    after = lifecycle.workdir / AFTER_CANCEL
    for path in (started, completion, after):
        if path.exists():
            path.unlink()

    try:
        task_id = client.submit(
            "Run this exact shell command with the Bash tool and do nothing else:\n\n"
            f"python -c \"import time; open('{CANCEL_STARTED}','w').write('started'); "
            f"time.sleep(120); open('{CANCEL_COMPLETION}','w').write('done')\"\n\n"
            "Do not finish early and do not create any other file.",
            model=FLASH_MODEL_ID,
            runtime=RUNTIME,
        )
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        emit("cancellation_stage", phase="target_submit_failed", detail=detail)
        return gate_record(False, phase="target_submit", detail=detail)
    emit("cancellation_stage", phase="target_submitted", task_id=task_id)

    # Answer approvals inline: the task will not reach a terminal state for ~120 s, so
    # `wait_status` cannot be used here -- it would block past the whole sleep and the completion
    # marker would already exist by the time the harness looked.
    deadline = time.monotonic() + 420
    started_seen = False
    answered: list[str] = []
    status = ""
    while time.monotonic() < deadline:
        task = client.task(task_id)
        status = task["status"]
        if status in TERMINAL:
            break
        if status == "waiting_for_approval":
            request_id, nested = client.pending_request(task_id)
            if request_id not in answered and "approve_once" in client.offered_decisions(nested):
                client.call(
                    "respond_agent_approval",
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "decision": "approve_once",
                    },
                )
                answered.append(request_id)
        if started.exists():
            started_seen = True
            break
        time.sleep(0.5)

    status_before_cancel = client.task(task_id)["status"]
    # Emitted before anything else can raise: a gate that reports only at the end loses the
    # attribution of whichever step failed, which is how a first-submit refusal and a
    # lease-not-released failure become the same line.
    emit(
        "cancellation_stage",
        phase="activity_observed",
        started_artifact_seen=started_seen,
        status_before_cancel=status_before_cancel,
        approvals_answered=len(answered),
    )
    cancelled = False
    final = status_before_cancel
    if started_seen and status_before_cancel not in TERMINAL:
        client.call("cancel_agent_task", {"task_id": task_id})
        final = client.wait_status(task_id, timeout=300, allow_approval=True)
        cancelled = final == "cancelled"

    completion_absent = not completion.exists()
    emit(
        "cancellation_stage",
        phase="cancelled",
        final_status=final,
        cancelled=cancelled,
        completion_artifact_absent=completion_absent,
    )
    # The writer lease and runtime liveness: a tiny write afterwards must succeed. A lease left
    # held by the cancelled task shows up here as WORKDIR_BUSY rather than as silence. Lease
    # release is asynchronous, so this retries for a bounded window and records the observed
    # delay -- "released after N seconds" and "never released" are different findings, and only
    # the second one is a failure.
    follow_status = None
    follow_error = None
    follow_delay_s = None
    settle_deadline = time.monotonic() + 30
    while True:
        try:
            follow = client.submit(
                f"Create {AFTER_CANCEL} containing exactly:\n\nstill-alive\n\n"
                "Do not modify any other file.",
                model=FLASH_MODEL_ID,
                runtime=RUNTIME,
            )
            follow_status = client.wait_status(follow, timeout=600, allow_approval=True)
            break
        except Exception as exc:  # noqa: BLE001 - the failure class is the result
            if "WORKDIR_BUSY" not in str(exc) or time.monotonic() >= settle_deadline:
                follow_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                break
            follow_delay_s = round(30 - (settle_deadline - time.monotonic()), 1)
            time.sleep(1.0)

    passed = (
        started_seen
        and status_before_cancel not in TERMINAL
        and cancelled
        and started.exists()
        and completion_absent
        and follow_status == "succeeded"
    )
    emit(
        "cancellation",
        started_artifact_seen=started_seen,
        status_before_cancel=status_before_cancel,
        final_status=final,
        cancelled=cancelled,
        started_still_present=started.exists(),
        completion_artifact_absent=completion_absent,
        follow_up_status=follow_status,
        follow_up_error=follow_error,
        lease_release_delay_s=follow_delay_s,
    )
    return gate_record(
        passed,
        started_artifact_seen=started_seen,
        status_before_cancel=status_before_cancel,
        final_status=final,
        cancelled=cancelled,
        completion_artifact_absent=completion_absent,
        follow_up_status=follow_status,
        follow_up_error=follow_error,
        lease_release_delay_s=follow_delay_s,
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
