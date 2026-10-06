"""Phase E acceptance run: launch the real chain, then drive §3-§9 through the public MCP surface.

Run as a script rather than a pytest module because this is an acceptance *execution*, not a unit
test: it talks to a real provider, takes minutes, and its result is evidence rather than an
assertion. The pytest suite keeps covering the deterministic contract; this produces the real
provider evidence Phase E closes on.

The preflight runs before the chain is launched and refuses to continue if its own conditions do
not hold, because every false conclusion in this phase came from a harness that measured something
other than what it claimed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase_e_acceptance import (  # noqa: E402
    AFTER_CANCEL_PROMPT,
    APPROVAL_PROMPT,
    APPROVAL_VARIANT,
    CANCEL_PROMPT,
    CONTINUATION_PROMPT,
    QUESTION_PROMPT,
    QUESTION_VARIANT,
    TERMINAL,
    WORKSPACE_WRITE_PROMPT,
    Acceptance,
    McpStdioClient,
    ToolError,
    native_ids,
)
from phase_e_lifecycle import (  # noqa: E402
    HarnessPreflightError,
    Lifecycle,
    process_command_lines,
    require_preflight,
    wait_until,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = (
    Path(__import__("os").environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)

WRITE_ARTIFACT = "phase-e-codex.txt"
WRITE_BYTES = b"serverfs-phase-e-codex"
CONTINUATION_ARTIFACT = "phase-e-continuation.txt"
CONTINUATION_BYTES = b"serverfs-phase-e-continuation"
QUESTION_ARTIFACT = "question-result.txt"
APPROVAL_ARTIFACT = "approval-result.txt"
CANCEL_ARTIFACT = "cancel-should-not-complete.txt"


def _alive(pid: int) -> bool:
    completed = subprocess.run(  # noqa: S603 - a fixed argv against an absolute path
        [
            str(POWERSHELL),
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue)"
            " { 'alive' } else { 'dead' }",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return "alive" in (completed.stdout or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary tree for inspection"
    )
    parser.add_argument("--gates", default="write,continue,model,question,approval,cancel")
    args = parser.parse_args()
    gates = {g.strip() for g in args.gates.split(",") if g.strip()}

    tmp_root = Path(tempfile.mkdtemp(prefix="phase-e-acceptance-"))
    env_file = REPO_ROOT / ".env"
    # The operator's real Codex home: a ChatGPT-signed-in CLI keeps its tokens in auth.json there,
    # and an empty home silently de-authenticates it.
    codex_home = Path.home() / ".codex"

    findings: dict[str, object] = {"workdir_is_repo_source": False}
    print(f"acceptance root: {tmp_root.name}", flush=True)

    # ---- §0 preflight ------------------------------------------------------------
    try:
        pre = require_preflight(env_file, codex_home)
    except HarnessPreflightError as exc:
        print(f"FAIL HARNESS: {exc}", flush=True)
        return 2
    findings["preflight"] = pre.summary()
    print(f"preflight: {json.dumps(pre.summary())}", flush=True)

    lifecycle = Lifecycle(tmp_root, env_file=env_file, codex_home=codex_home)
    findings["workdir_is_repo_source"] = REPO_ROOT in lifecycle.workdir.parents
    baseline_codex = _codex_baseline()
    print(f"codex baseline pids: {len(baseline_codex)}", flush=True)

    try:
        lifecycle.launch()
        client = McpStdioClient(lifecycle)
        client.initialize()
        names = client.tool_names()
        findings["agent_tool_count"] = len([n for n in names if "agent" in n])
        print(f"agent tools published: {findings['agent_tool_count']}", flush=True)

        acc = Acceptance(client, lifecycle)
        findings.update(_run_gates(client, lifecycle, acc, gates))
        findings["findings"] = acc.findings
    finally:
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            lifecycle.kill()

    # ---- §17 process cleanup --------------------------------------------------
    wait_until(lambda: not lifecycle.codex_app_server_pids(), timeout=30)
    findings["bridge_owned_app_server_gone"] = not lifecycle.codex_app_server_pids()
    findings["codex_back_to_baseline"] = len(_codex_baseline()) == len(baseline_codex)

    print(json.dumps(findings, indent=2, ensure_ascii=False), flush=True)
    if not args.keep:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return 0


def _codex_baseline() -> list[int]:
    """Every live codex.exe, used only to compare before/after. Never acted upon."""
    pids = []
    for pid, command_line in process_command_lines():
        if "codex" in command_line.lower() and "powershell" not in command_line.lower():
            pids.append(pid)
    return pids


def _run_gates(
    client: McpStdioClient, lifecycle: Lifecycle, acc: Acceptance, gates: set[str]
) -> dict[str, object]:
    results: dict[str, object] = {}
    disk = lifecycle.workdir

    # ---- §3 workspace-write ---------------------------------------------------
    if "write" in gates:
        assert not (disk / WRITE_ARTIFACT).exists(), "harness must not pre-create the artifact"
        task_id = client.submit(WORKSPACE_WRITE_PROMPT)
        status = client.wait_status(task_id, timeout=900)
        acc.record("write_status", status)
        acc.record("write_final_response", client.task(task_id).get("final_response"))
        # A real approval the provider asked for, answered through the public tool. Recorded so the
        # §8 evidence is a count of genuine provider requests rather than an assumption.
        acc.record("write_approvals_observed", len(client.observed_approvals.get(task_id, [])))
        if client.approval_failures.get(task_id):
            acc.record("write_approval_failures", client.approval_failures[task_id])
        if client.missing_pending_id.get(task_id):
            acc.record("write_missing_pending_id", client.missing_pending_id[task_id])
        acc.record("write_artifact_on_disk", (disk / WRITE_ARTIFACT).exists())
        acc.record(
            "write_artifact_exact",
            (disk / WRITE_ARTIFACT).read_bytes().strip() == WRITE_BYTES
            if (disk / WRITE_ARTIFACT).exists()
            else False,
        )
        acc.record("write_public_read", client.read_file(WRITE_ARTIFACT))
        acc.record("write_events", client.event_types(task_id))
        acc.record("write_native_ids", native_ids(client, lifecycle, task_id))
        results["write_task_id"] = task_id
        print(f"§3 write: {acc.findings['write_status']}", flush=True)

    if "continue" in gates:
        first = results.get("write_task_id")
        if first:
            task_id = client.submit(CONTINUATION_PROMPT, continue_from=first)
            status = client.wait_status(task_id, timeout=900)
            acc.record("continuation_status", status)
            acc.record(
                "continuation_artifact_exact",
                (disk / CONTINUATION_ARTIFACT).read_bytes().strip() == CONTINUATION_BYTES
                if (disk / CONTINUATION_ARTIFACT).exists()
                else False,
            )
            acc.record("continuation_native_ids", native_ids(client, lifecycle, task_id))
            acc.record("continuation_events", client.event_types(task_id))
            results["continuation_task_id"] = task_id
            print(f"§5 continuation: {status}", flush=True)

    # ---- §6 model override ----------------------------------------------------
    if "model" in gates:
        results.update(_model_override(client, lifecycle, acc))

    # ---- §7 question ----------------------------------------------------------
    if "question" in gates:
        results["question"] = _question(client, lifecycle, acc)

    # ---- §8 approval ----------------------------------------------------------
    if "approval" in gates:
        results["approval"] = _approval(client, lifecycle, acc)

    # ---- §9 cancellation ------------------------------------------------------
    if "cancel" in gates:
        results["cancel"] = _cancel(client, lifecycle, acc)

    return results


def _model_override(client: McpStdioClient, lifecycle: Lifecycle, acc: Acceptance) -> dict:
    """§6: a live-catalog model, then a task without one.

    The second task is the point: it proves the override was request-scoped rather than becoming a
    ServerFS default or touching the provider's own default model.
    """
    catalog = client.call("list_agent_models", {})
    models = catalog.get("models", [])
    # A live selection, never a hardcoded id: the catalog is a dated snapshot, not a contract.
    chosen = next(
        (m for m in models if m.get("enabled") and not m.get("hidden")),
        models[0] if models else None,
    )
    acc.record("model_catalog_count", len(models))
    if chosen is None:
        return {"model_override": "no model in the live catalog"}
    model_id = chosen.get("id")
    acc.record("model_selected", model_id)

    with_model = client.submit("Reply exactly:\n\nmodel-ok\nDo not use tools.", model=model_id)
    status_with = client.wait_status(with_model, timeout=900)
    task_with = client.task(with_model)
    acc.record("model_task_status", status_with)
    acc.record("model_task_response", (task_with.get("final_response") or "").strip())
    acc.record("model_task_recorded_request", task_with.get("requested_model"))

    without_model = client.submit("Reply exactly:\n\nmodel-ok\nDo not use tools.")
    status_without = client.wait_status(without_model, timeout=900)
    task_without = client.task(without_model)
    acc.record("model_omitted_status", status_without)
    acc.record("model_omitted_inherited", task_without.get("requested_model"))
    return {"model_task_id": with_model, "model_omitted_task_id": without_model}


def _question(client: McpStdioClient, lifecycle: Lifecycle, acc: Acceptance) -> dict:
    """§7: a real provider question, answered through the public tool.

    If the provider does not emit one, one bounded prompt variant is tried and then the gate
    reports the absence honestly. Codex persistent config and provider authority are never touched
    to force it.
    """
    for label, prompt in (("primary", QUESTION_PROMPT), ("variant", QUESTION_VARIANT)):
        task_id = client.submit(prompt)
        try:
            task = client.wait_for(task_id, {"waiting_for_question", *TERMINAL}, timeout=900)
        except TimeoutError:
            acc.record(f"question_{label}_timeout", True)
            continue
        if task["status"] != "waiting_for_question":
            acc.record(f"question_{label}_status", task["status"])
            continue
        request_id = client.pending_request_id(task_id)
        # Provenance: the event stream must show the provider asking, not a harness-injected event.
        events = client.events(task_id)
        acc.record(
            f"question_{label}_events",
            sorted({str(e.get("event_type")) for e in events}),
        )
        acc.record(f"question_{label}_pending_request_id_present", bool(request_id))
        try:
            client.call(
                "answer_agent_question",
                {
                    "task_id": task_id,
                    "request_id": request_id,
                    "answers": [
                        {"question_id": "q1", "answers": ["beta"]},
                    ],
                },
            )
        except ToolError as exc:
            acc.record(f"question_{label}_answer_error", str(exc)[:300])
            continue
        final = client.wait_status(task_id, timeout=900)
        acc.record(f"question_{label}_status", final)
        acc.record(
            f"question_{label}_artifact",
            (lifecycle.workdir / QUESTION_ARTIFACT).read_text().strip()
            if (lifecycle.workdir / QUESTION_ARTIFACT).exists()
            else None,
        )
        if final == "succeeded":
            return {"genuine": True, "variant": label, "task_id": task_id}
    return {"genuine": False, "reason": "provider emitted no requestUserInput"}


def _approval(client: McpStdioClient, lifecycle: Lifecycle, acc: Acceptance) -> dict:
    """§8: a real provider approval, answered through the public tool.

    Never forced by adjusting Codex persistent approval policy, injecting an approvalPolicy into
    thread/start, or changing sandbox authority.
    """
    for label, prompt in (("primary", APPROVAL_PROMPT), ("variant", APPROVAL_VARIANT)):
        task_id = client.submit(prompt)
        try:
            task = client.wait_for(task_id, {"waiting_for_approval", *TERMINAL}, timeout=900)
        except TimeoutError:
            acc.record(f"approval_{label}_timeout", True)
            continue
        if task["status"] != "waiting_for_approval":
            acc.record(f"approval_{label}_status", task["status"])
            continue
        request_id = client.pending_request_id(task_id)
        acc.record(
            f"approval_{label}_events",
            sorted({str(e.get("event_type")) for e in client.events(task_id)}),
        )
        try:
            client.call(
                "respond_agent_approval",
                {
                    "task_id": task_id,
                    "request_id": request_id,
                    "decision": "approve_once",
                },
            )
        except ToolError as exc:
            acc.record(f"approval_{label}_respond_error", str(exc)[:300])
            continue
        final = client.wait_status(task_id, timeout=900)
        acc.record(f"approval_{label}_status", final)
        acc.record(
            f"approval_{label}_artifact",
            (lifecycle.workdir / APPROVAL_ARTIFACT).read_text().strip()
            if (lifecycle.workdir / APPROVAL_ARTIFACT).exists()
            else None,
        )
        if final == "succeeded":
            return {"genuine": True, "variant": label, "task_id": task_id}
    return {"genuine": False, "reason": "provider emitted no approval request"}


def _cancel(client: McpStdioClient, lifecycle: Lifecycle, acc: Acceptance) -> dict:
    """§9: cancel only after the provider has visibly started the turn.

    Not a sleep-race: the event stream must show a started item first, so the interrupt lands on a
    live turn rather than on a submit that has not been picked up yet.
    """
    task_id = client.submit(CANCEL_PROMPT)
    # Wait for provider-side activity before cancelling.
    deadline = time.monotonic() + 300
    saw_activity = False
    while time.monotonic() < deadline:
        types = client.event_types(task_id)
        if any("item.started" in t or "turn.started" in t for t in types):
            saw_activity = True
            break
        if client.task(task_id)["status"] in TERMINAL:
            break
        time.sleep(0.5)
    acc.record("cancel_saw_provider_activity", saw_activity)
    if not saw_activity:
        return {"cancelled": False, "reason": "no provider-side activity observed"}

    status_before = client.task(task_id)["status"]
    try:
        client.call("cancel_agent_task", {"task_id": task_id})
    except ToolError as exc:
        acc.record("cancel_error", str(exc)[:300])
        return {"cancelled": False}
    final = client.wait_status(task_id, timeout=300)
    acc.record("cancel_status_before", status_before)
    acc.record("cancel_final_status", final)
    acc.record("cancel_artifact_absent", not (lifecycle.workdir / CANCEL_ARTIFACT).exists())

    after = client.submit(AFTER_CANCEL_PROMPT)
    after_status = client.wait_status(after, timeout=900)
    acc.record("after_cancel_status", after_status)
    acc.record("after_cancel_response", (client.task(after).get("final_response") or "").strip())
    return {"cancelled": final in {"cancelled", "interrupted"}, "task_id": task_id}


if __name__ == "__main__":
    raise SystemExit(main())
