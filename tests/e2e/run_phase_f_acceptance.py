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
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from phase_e_acceptance import McpStdioClient, ToolError  # noqa: E402
from phase_e_lifecycle import (  # noqa: E402
    Lifecycle,
    load_env_file,
    require_file_stderr,
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


async def live_model_gate(client: McpStdioClient) -> dict[str, Any]:
    """Re-assert the free-model gate against the live catalog, before any task is submitted."""
    catalog = client.call("list_agent_models", {"runtime": RUNTIME})
    entries = catalog.get("models") or []
    # An empty catalog has two very different causes, and conflating them produces a fictional
    # diagnosis: the runtime may be unavailable (no CLI, not signed in), or discovery may have
    # failed while the runtime itself is fine. `status` and `detail` are the Bridge's own statement
    # of which, so an empty list is never reported as "the model is gone" without them.
    if not entries:
        return {
            "gate": "STOP",
            "clause": "CATALOG_EMPTY",
            "runtime_status": catalog.get("status"),
            "detail": str(catalog.get("detail") or "")[:120],
            "catalog_count": 0,
        }
    match = next((m for m in entries if (m.get("id") or m.get("modelId")) == FLASH_MODEL_ID), None)
    if match is None:
        return {"gate": "STOP", "clause": "ABSENT", "catalog_count": len(entries)}
    enabled = match.get("isEnabled", match.get("enabled"))
    free = match.get("isFree", match.get("free"))
    price = match.get("priceFactor")
    clause = (
        "FREE_AND_ENABLED"
        if enabled is True and free is True and not (isinstance(price, (int, float)) and price > 0)
        else ("NOT_ENABLED" if enabled is not True else "NOT_FREE")
    )
    return {
        "gate": "GO" if clause == "FREE_AND_ENABLED" else "STOP",
        "clause": clause,
        "catalog_count": len(entries),
        "enabled": enabled,
        "is_free": free,
        "price_factor": price,
    }


def read_probe(workdir: Path) -> dict[str, bool] | None:
    path = workdir / PROBE_ARTIFACT
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def main() -> int:
    endpoint = agent_endpoint()
    emit("setup", runtime=RUNTIME, endpoint_exposed=False, model=FLASH_MODEL_ID)

    require_preflight(REPO_ROOT / ".env", Path.home() / ".codex")
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
    results: dict[str, Any] = {}
    try:
        lifecycle.launch()
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
                clause=gate["clause"],
                implication=(
                    "The free model is gone, disabled or no longer free. Do not substitute "
                    "Qwen3.8-Max: paying for a model this acceptance was not approved to use is "
                    "not a decision this run may make."
                ),
            )
            return 3

        results["probe"] = await _gate(
            client, "probe", "agent_runtime_status", {"runtime": RUNTIME}
        )
        results["model_discovery"] = {
            "catalog_count": gate["catalog_count"],
            "model_present": True,
        }

        # --- E1 / E2: the tool-environment scrub -------------------------------------------
        results["e1"] = await _e1_non_vacuity(client, lifecycle, endpoint)
        if not results["e1"].get("non_vacuous"):
            emit(
                "verdict",
                answer="STOP_E1_VACUOUS",
                implication=(
                    "The tool saw none of the sentinels, so E2 would prove nothing. Do not read "
                    "the scrub as working."
                ),
            )
            return 4
        results["e2"] = await _e2_product_scrub(client, lifecycle)

        results["workspace_write"] = await _workspace_write(client, lifecycle)
        results["continuation"] = await _continuation(client, results)
        results["approval"] = await _approval(client)
        results["question"] = await _question(client)
        results["model_override"] = await _model_override(client, results)
        results["cancellation"] = await _cancellation(client)
    except ToolError as exc:
        emit("tool_error", detail=str(exc)[:200])
        results["tool_error"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        emit("run_failed", error_class=type(exc).__name__)
        results["run_failed"] = type(exc).__name__
    finally:
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            lifecycle.kill()
        results["bridge_owned_gone"] = not lifecycle.codex_app_server_pids()
        try:
            qoder_pids = lifecycle.bridge_pids()
        except Exception:  # noqa: BLE001
            qoder_pids = []
        results["bridge_stopped"] = not qoder_pids
        shutil.rmtree(tmp_root, ignore_errors=True)
        emit(
            "cleanup",
            **results.get("cleanup", {})
            or {
                "bridge_owned_gone": results.get("bridge_owned_gone"),
                "bridge_stopped": results.get("bridge_stopped"),
            },
        )

    emit("results", **{k: v for k, v in results.items() if k != "findings"})
    failed = [name for name, value in results.items() if value is None or value is False]
    emit(
        "verdict",
        answer="F3_PARTIAL" if failed else "F3_PASS",
        failed=failed,
        implication=(
            "Every gate in this run passed through the public MCP surface against the real "
            "provider."
            if not failed
            else "Some gates did not pass. Each is reported as measured; none is inferred."
        ),
    )
    return 0 if not failed else 5


async def _gate(client: McpStdioClient, label: str, tool: str, arguments: dict) -> Any:
    try:
        result = client.call(tool, arguments)
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        emit(label, ok=False, error_class=type(exc).__name__)
        return None
    emit(label, ok=True)
    return result


async def _e1_non_vacuity(
    client: McpStdioClient, lifecycle: Lifecycle, endpoint: str
) -> dict[str, Any]:
    """Show the tool really can see proxy state, so E2's absence means something.

    Run through the public surface with the sentinels installed in the *harness* environment,
    which is what the Bridge process inherits. No deletion overlay exists on this path, so a name
    that is set here must reach the tool.
    """
    for name in SCRUB_NAMES:
        os.environ[name] = endpoint
    try:
        task_id = client.submit(ENV_PROBE_PROMPT, runtime=RUNTIME)
        status = client.wait_status(task_id, timeout=900, allow_approval=True)
    finally:
        for name in SCRUB_NAMES:
            os.environ.pop(name, None)

    probe = read_probe(lifecycle.workdir)
    visible = sorted(n for n, present in (probe or {}).items() if present)
    emit(
        "e1",
        status=status,
        probe_present=probe is not None,
        visible_to_tool=visible,
        non_vacuous=bool(visible),
    )
    return {"status": status, "probe_present": probe is not None, "non_vacuous": bool(visible)}


async def _e2_product_scrub(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """The same probe, through the product chain, where the deletion overlay applies.

    This is the leg F0 could not close. It goes through `submit_agent_task` like any other task,
    so the environment the tool sees is whatever ServerFS decided it should be -- not something the
    harness arranged.
    """
    probe_path = lifecycle.workdir / PROBE_ARTIFACT
    if probe_path.exists():
        probe_path.unlink()

    task_id = client.submit(ENV_PROBE_PROMPT, runtime=RUNTIME)
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    probe = read_probe(lifecycle.workdir)
    visible = sorted(n for n, present in (probe or {}).items() if present)
    emit(
        "e2",
        status=status,
        probe_present=probe is not None,
        visible_to_tool=visible,
        all_absent=probe is not None and not visible,
    )
    return {
        "status": status,
        "probe_present": probe is not None,
        "all_absent": probe is not None and not visible,
        "task_succeeded": status == "succeeded",
    }


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
    record = client.task(task_id)
    ids = {
        "has_native_session": bool(record.get("native_session_id")),
        "has_native_turn": bool(record.get("native_turn_id")),
    }
    emit("workspace_write", status=status, artifact_exact=exact, **ids)
    return {"status": status, "artifact_exact": exact, **ids, "task_id": task_id}


async def _continuation(client: McpStdioClient, results: dict[str, Any]) -> dict[str, Any]:
    source = results.get("workspace_write", {}).get("task_id")
    if not source:
        return {"status": None, "reason": "no source task"}
    task_id = client.submit(
        "Create phase-f3-continuation.txt containing exactly:\n\nqoder-continuation\n\n"
        "Do not modify any other file.",
        continue_from=source,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    emit("continuation", status=status)
    return {"status": status, "task_id": task_id}


async def _approval(client: McpStdioClient) -> dict[str, Any]:
    task_id = client.submit(
        "Create phase-f3-approval.txt containing exactly:\n\napproved\n\n"
        "Do not modify any other file.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    approvals = 1 if status == "succeeded" else 0
    emit("approval", status=status, approvals_observed=bool(approvals))
    return {"status": status, "approvals_observed": bool(approvals)}


async def _question(client: McpStdioClient) -> dict[str, Any]:
    task_id = client.submit(
        "Before making any workspace change, ask me one question using your user-input mechanism "
        "and wait for my answer.\n\nAsk: Which marker should I write?\n\nOffer exactly two "
        "choices: alpha, beta.\n\nAfter I answer, create phase-f3-question.txt containing "
        "exactly the selected marker. Do not modify any other file.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=True)
    events = client.event_types(task_id)
    genuine = any("question" in name for name in events)
    emit("question", status=status, provider_question_observed=genuine)
    return {"status": status, "provider_question_observed": genuine}


async def _model_override(client: McpStdioClient, results: dict[str, Any]) -> dict[str, Any]:
    task_id = client.submit(
        "Reply with exactly:\n\nqoder-model-ok\n\nDo not use tools.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=900, allow_approval=False)
    record = client.task(task_id)
    requested = record.get("requested_model")
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
    )
    return {
        "override_status": status,
        "override_recorded": requested == FLASH_MODEL_ID,
        "default_status": default_status,
        "default_inherited": inherited,
        "nothing_inherited": inherited is None,
    }


async def _cancellation(client: McpStdioClient) -> dict[str, Any]:
    task_id = client.submit(
        "Run a shell command in this workspace that waits about 120 seconds and only afterwards "
        "creates phase-f3-cancel-should-not-exist.txt containing the word done. Do not finish "
        "early.",
        model=FLASH_MODEL_ID,
        runtime=RUNTIME,
    )
    deadline = time.monotonic() + 420
    saw_activity = False
    while time.monotonic() < deadline:
        types = client.event_types(task_id)
        if any("item.started" in name or "turn.started" in name for name in types):
            saw_activity = True
            break
        if client.task(task_id)["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            break
        time.sleep(0.5)
    client.call("cancel_agent_task", {"task_id": task_id})
    final = client.wait_status(task_id, timeout=300, allow_approval=False)
    emit("cancellation", saw_activity=saw_activity, final_status=final)
    return {"saw_activity": saw_activity, "final_status": final}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
