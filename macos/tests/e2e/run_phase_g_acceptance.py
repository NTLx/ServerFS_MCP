"""G2: real-provider acceptance for the Claude runtime, through the public MCP surface only.

Three layers, run as separate phases so a real failure pauses the dependent work instead of
stacking results (maintainer ruling, 2026-10-08):

    --phase 1  probe -> direct real turn (use_proxy=false) -> session creation
    --phase 2  paired proxy turn: use_proxy=true with a local credentialless CONNECT forwarder
    --phase 3  file mutation -> continuation -> approval -> question -> interrupt -> cancellation
               -> recovery classification -> cleanup

Rules this driver enforces rather than assumes:

* Every gate states its own ``passed`` boolean; the verdict reads nothing else. ``not_run`` and
  aborted phases are first-class outcomes and can never summarise into a PASS.
* The model is never specified: the provider-native default is the approved configuration, and
  the maintainer explicitly ruled out creating a ServerFS-owned credential path. No live model
  gate exists because Claude's discovery is honestly ``unsupported``.
* Approvals and questions must be produced by the real provider. If a bounded set of prompts does
  not surface one, the gate records the absence honestly instead of adjusting provider settings
  to manufacture one.
* ``interrupt`` and ``cancellation`` are two gates over one cancelled task, but their evidence is
  deliberately disjoint: the interrupt gate claims the *provider activity stopped* (the claude
  child disappears and the completion artifact is never written); the cancellation gate claims
  the *ServerFS task semantics held* (terminal ``cancelled``, lease released so a follow-up task
  succeeds). Neither is derived from the other.
* Nothing prints a proxy endpoint, a token, an account identifier, an ``ANTHROPIC_*`` value, or a
  native identifier. Presence booleans, counts and process names are what get recorded.
* The acceptance config is rendered into a temp root; the resident tunnel's ``serverfs.toml`` is
  never touched, and the resident tunnel has no ``[agent]`` section, so it holds no lifecycle
  lease this run could collide with.
"""

from __future__ import annotations

import argparse
import json
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
    Lifecycle,
    load_env_file,
    require_file_stderr,
    require_own_bridge,
)
from phase_e_restart_reconcile import (  # noqa: E402
    _mutation_allowed,
    _read_native,
    _start_bystander,
    _terminate_abruptly,
    _wait_gone,
)

RUNTIME = "claude"

#: Long-running command gates. The sleep is long enough that "cancelled mid-turn" and "finished on
#: its own" are separable by the completion marker alone, with no fixed sleeps in the harness.
INTERRUPT_STARTED = "phase-g-interrupt-started.txt"
INTERRUPT_COMPLETION = "phase-g-interrupt-should-not-exist.txt"
AFTER_CANCEL = "phase-g-after-cancel.txt"

RECOVERY_STARTED = "phase-g-recovery-started.txt"
RECOVERY_COMPLETION = "phase-g-recovery-should-not-exist.txt"
RECOVERY_AFTER = "phase-g-recovery-resumed.txt"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: Bounded windows. These are ceilings for bounded polling, never readiness sleeps.
TURN_TIMEOUT_S = 900.0
CHILD_EXIT_WINDOW_S = 30.0
CONTAINMENT_WAIT_S = 30.0

EXPECTED_GATES: tuple[str, ...] = (
    "ownership",
    "probe",
    "direct_turn",
    "session_creation",
    "direct_cleanup",
    "proxy_ownership",
    "proxy_turn",
    "proxy_cleanup",
    "file_mutation",
    "continuation",
    "approval",
    "question",
    "interrupt",
    "cancellation",
    "recovery",
    "cleanup",
)

PHASE_GATES: dict[int, tuple[str, ...]] = {
    1: ("ownership", "probe", "direct_turn", "session_creation", "direct_cleanup"),
    2: ("proxy_ownership", "proxy_turn", "proxy_cleanup"),
    3: (
        "file_mutation",
        "continuation",
        "approval",
        "question",
        "interrupt",
        "cancellation",
        "recovery",
        "cleanup",
    ),
}


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def gate_record(passed: bool, **evidence: Any) -> dict[str, Any]:
    """One gate's outcome. ``passed`` is the only field the verdict reads."""
    return {"passed": bool(passed), **evidence}


def _verdict(results: dict[str, Any], phase: int) -> dict[str, Any]:
    """Nothing but an explicit per-gate pass can read as PASS; missing and aborted never do."""
    expected = PHASE_GATES[phase]
    not_run = [name for name in expected if name not in results]
    aborted = [name for name in ("run_failed", "tool_error", "harness_failure") if name in results]
    failed = [
        name
        for name in expected
        if name in results
        if not (isinstance(results[name], dict) and results[name].get("passed") is True)
    ]
    ok = not failed and not not_run and not aborted
    return {
        "answer": f"G2_PHASE{phase}_PASS" if ok else f"G2_PHASE{phase}_PARTIAL",
        "failed": failed,
        "not_run": not_run,
        "aborted": aborted,
        "implication": (
            "Every gate in this phase passed through the public MCP surface against the real "
            "provider."
            if ok
            else "Not every expected gate completed with an explicit pass; none is inferred."
        ),
    }


def _run_gate(name: str, thunk: Any) -> dict[str, Any]:
    """Run one gate; an exception inside it fails that gate, not the whole run.

    The gate body arrives as a thunk on purpose: a directly-called gate would evaluate (and
    possibly raise) *before* entering this function, which is exactly how a gate failure used to
    become a run failure.
    """
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 - the failure class is the gate's result
        emit("gate_failed", gate=name, error_class=type(exc).__name__, detail=str(exc)[:250])
        return gate_record(False, error_class=type(exc).__name__, detail=str(exc)[:250])


# -- Claude-specific preflight -------------------------------------------------------------------


def _resolve_claude_exe() -> str | None:
    configured = "claude"
    from_path = shutil_which(configured)
    for candidate in (from_path, str(Path.home() / ".local" / "bin" / "claude.EXE")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def shutil_which(name: str) -> str | None:

    return shutil.which(name)


def claude_preflight() -> dict[str, Any]:
    """Resolve the CLI and prove the frozen SDK pin imports under the Bridge interpreter.

    The provider-auth round trip itself was measured read-only before this driver was allowed to
    start (connect -> get_server_info -> interrupt -> disconnect, 2026-10-08); here only the
    prerequisites the driver can check without a turn are re-asserted.
    """
    resolved = _resolve_claude_exe()
    check_sdk = subprocess.run(  # noqa: S603 - fixed argv from a constant
        [
            str(BRIDGE_PYTHON),
            "-c",
            "import claude_agent_sdk as s; print(getattr(s, '__version__', 'unknown'))",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    pin = check_sdk.stdout.strip().splitlines()[-1] if check_sdk.returncode == 0 else None
    ok = resolved is not None and pin is not None
    emit("claude_preflight", cli_resolved=resolved is not None, sdk_pin_imported=pin is not None)
    return {"ok": ok, "sdk_version": pin}


# -- Claude child process observation -------------------------------------------------------------


def _claude_child_pids() -> list[int]:
    """PIDs whose image is the Claude CLI itself.

    Matched on the image name rather than a substring of the command line, so an unrelated
    process that merely mentions claude in an argument never counts. Callers diff this against a
    baseline instead of trusting an absolute list: the operator may legitimately run their own
    Claude session, and it must never be attributed to (or killed by) this chain.
    """
    from phase_e_lifecycle import process_command_lines

    out = []
    for pid, command_line in process_command_lines():
        image = Path(command_line.split()[0]).name.lower() if command_line.split() else ""
        if image in {"claude.exe", "claude"}:
            out.append(pid)
    return out


def _wait_child_gone(pids: list[int], timeout: float) -> tuple[bool, list[int]]:
    return _wait_gone(pids, timeout)


# -- The credentialless CONNECT forwarder (proxy arm) ---------------------------------------------


class ConnectForwarder:
    """A local HTTP forwarder that answers CONNECT and relays the tunnel, recording targets.

    Credentialless by construction: it holds no upstream credential and adds none. The evidence
    it produces is counts and booleans -- a CONNECT target hostname is an endpoint fact and never
    enters the record, only whether it was loopback or external.

    Process attribution: for each observed CONNECT, the TCP connections whose *remote* side is
    this listener are queried once, so the client process of the tunnel is identified from the
    OS rather than inferred from the traffic shape.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="g2-forwarder")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _accept_loop(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_conn, args=(conn,), daemon=True, name="g2-forwarder-conn"
            ).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            target = parts[1]
            host, _, port_raw = target.rpartition(":")
            loopback = host.lower() in LOOPBACK_HOSTS
            attribution = _attribute_tunnel_clients(self.port or 0)
            with self._lock:
                self.records.append(
                    {
                        "target": target,
                        "loopback": loopback,
                        "external": not loopback,
                        "client_pids": [pid for pid, _name in attribution],
                        "client_images": sorted({name for _pid, name in attribution}),
                    }
                )
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._relay(conn, host, int(port_raw or 443))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _relay(client: socket.socket, host: str, port: int) -> None:
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError:
            return
        sockets = [client, upstream]
        try:
            while True:
                readable, _w, _x = select.select(sockets, [], [], 60)
                if not readable:
                    continue
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        except OSError:
            pass
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.records)
        external = [r for r in records if r["external"]]
        loopback = [r for r in records if r["loopback"]]
        attributed = [r for r in external if r["client_pids"]]
        images = sorted({img for r in external for img in r["client_images"]})
        return {
            "connect_count": len(records),
            "external_connect_count": len(external),
            "loopback_target_count": len(loopback),
            "external_attributed_count": len(attributed),
            "client_images_observed": images,
        }


def _attribute_tunnel_clients(listen_port: int) -> list[tuple[int, str]]:
    """Which local process holds a connection *to* this listener, from the OS, once per CONNECT."""
    if not listen_port:
        return []
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv with one integer
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-NetTCPConnection -RemotePort "
                f"{listen_port} -State Established -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique)",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    named: list[tuple[int, str]] = []
    for pid in pids[:4]:
        proc = subprocess.run(  # noqa: S603 - fixed argv with one integer
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        name = (proc.stdout or "").strip().splitlines()
        named.append((pid, name[0] if name else "unknown"))
    return named


# -- Layer 1 gates --------------------------------------------------------------------------------


def _probe(client: McpStdioClient) -> dict[str, Any]:
    """G2-01: the public runtime listing must show Claude available, via the real CLI probe."""
    try:
        listing = client.call("list_agent_runtimes", {})
    except Exception as exc:  # noqa: BLE001 - the failure class is the result
        return gate_record(False, error_class=type(exc).__name__, detail=str(exc)[:200])
    runtimes = listing.get("runtimes") or listing.get("agents") or []
    claude = next((r for r in runtimes if isinstance(r, dict) and r.get("name") == RUNTIME), None)
    available = bool(claude and claude.get("available"))
    version_present = bool(claude and claude.get("version"))
    return gate_record(
        available,
        claude_listed=claude is not None,
        available=available,
        version_present=version_present,
        runtime_count=len(runtimes),
    )


def _artifact_task(
    client: McpStdioClient,
    lifecycle: Lifecycle,
    *,
    artifact: str,
    marker: str,
    continue_from: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """One real workspace-write turn that must produce an exact artifact. Shared infrastructure."""
    path = lifecycle.workdir / artifact
    if path.exists():
        path.unlink()
    prompt = f"Create {artifact} containing exactly:\n\n{marker}\n\nDo not modify any other file."
    task_id = client.submit(prompt, runtime=RUNTIME, continue_from=continue_from)
    status = client.wait_status(task_id, timeout=TURN_TIMEOUT_S, allow_approval=True)
    content = path.read_text(encoding="utf-8").strip() if path.exists() else None
    if status != "succeeded":
        # Preserve the failure's own words before any teardown can destroy the scene: the task's
        # redacted error fields plus the chain's stderr tail are the difference between an
        # attributable gate failure and an unexplained one (the Phase E lesson, re-learned here).
        record = client.task(task_id)
        emit(
            "task_error",
            task_id=task_id,
            error_code=record.get("error_code"),
            error_message=(record.get("error_message") or "")[:300],
            stderr_tail=lifecycle.stderr_text()[-1500:],
        )
    return task_id, status, {"artifact_exact": content == marker, "artifact_content": content}


def _direct_turn(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """G2-02A: a real turn with use_proxy=false -- direct egress is the deployment's own answer."""
    task_id, status, artifact = _artifact_task(
        client, lifecycle, artifact="phase-g-direct.txt", marker="claude-direct-turn"
    )
    passed = status == "succeeded" and artifact["artifact_exact"]
    emit("direct_turn", status=status, task_id=task_id, **artifact)
    return gate_record(passed, status=status, task_id=task_id, **artifact)


def _session_creation(client: McpStdioClient, lifecycle: Lifecycle, task_id: str) -> dict[str, Any]:
    """G2-03: the native session exists in the TaskStore and the public projection hides it."""
    store = native_ids(client, lifecycle, task_id)
    public = client.task(task_id)
    public_absent = "native_session_id" not in public and "native_turn_id" not in public
    passed = store["thread_id_present"] and store["turn_id_present"] and public_absent
    emit(
        "session_creation",
        thread_id_present=store["thread_id_present"],
        turn_id_present=store["turn_id_present"],
        store_found=store.get("store_found"),
        public_projection_hides_native_ids=public_absent,
    )
    return gate_record(
        passed,
        thread_id_present=store["thread_id_present"],
        turn_id_present=store["turn_id_present"],
        public_projection_hides_native_ids=public_absent,
    )


def _proxy_turn(client: McpStdioClient, lifecycle: Lifecycle, forwarder: ConnectForwarder) -> dict:
    """G2-02B: a real turn whose egress must traverse the forwarder and be attributable.

    Passing requires all four maintainer conditions: the turn succeeds, the forwarder observed an
    external CONNECT, the tunnel client is attributed to the claude child from the OS, and no
    target was loopback. A direct-reachable endpoint is not a failure of this gate -- that answer
    belongs to the paired direct arm.
    """
    task_id, status, artifact = _artifact_task(
        client, lifecycle, artifact="phase-g-proxy.txt", marker="claude-proxy-turn"
    )
    summary = forwarder.summary()
    passed = (
        status == "succeeded"
        and artifact["artifact_exact"]
        and summary["external_connect_count"] > 0
        and summary["external_attributed_count"] > 0
        and any("claude" in img.lower() for img in summary["client_images_observed"])
        and summary["loopback_target_count"] == 0
    )
    emit("proxy_turn", status=status, task_id=task_id, **artifact, **summary)
    return gate_record(passed, status=status, task_id=task_id, **artifact, **summary)


# -- Layer 2 gates --------------------------------------------------------------------------------


def _file_mutation(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """A second, independently judged real write (the source task for continuation)."""
    task_id, status, artifact = _artifact_task(
        client, lifecycle, artifact="phase-g-mutation.txt", marker="claude-file-mutation"
    )
    passed = status == "succeeded" and artifact["artifact_exact"]
    emit("file_mutation", status=status, task_id=task_id, **artifact)
    return gate_record(passed, status=status, task_id=task_id, **artifact)


def _continuation(
    client: McpStdioClient, lifecycle: Lifecycle, source_task_id: str
) -> dict[str, Any]:
    task_id, status, artifact = _artifact_task(
        client,
        lifecycle,
        artifact="phase-g-continuation.txt",
        marker="claude-continuation",
        continue_from=source_task_id,
    )
    identity = compare_native_ids(lifecycle, source_task_id, task_id)
    passed = status == "succeeded" and artifact["artifact_exact"] and all(identity.values())
    emit("continuation", status=status, task_id=task_id, **artifact, **identity)
    return gate_record(passed, status=status, task_id=task_id, **artifact, **identity)


def _approval(client: McpStdioClient) -> dict[str, Any]:
    task_id = client.submit(
        "Create phase-g-approval.txt containing exactly:\n\napproved\n\n"
        "Do not modify any other file.",
        runtime=RUNTIME,
    )
    status = client.wait_status(task_id, timeout=TURN_TIMEOUT_S, allow_approval=True)
    events = client.event_types(task_id)
    requested = any("approval.requested" in n for n in events)
    resolved = any("approval.resolved" in n for n in events)
    answered = len(client.observed_approvals.get(task_id, [])) > 0
    body = client.read_file("phase-g-approval.txt")
    passed = status == "succeeded" and requested and resolved and answered
    emit(
        "approval",
        status=status,
        approval_requested=requested,
        approval_resolved=resolved,
        approvals_observed=answered,
        artifact_present=body is not None,
    )
    return gate_record(
        passed,
        status=status,
        approval_requested=requested,
        approval_resolved=resolved,
        approvals_observed=answered,
    )


def _question(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    artifact_path = lifecycle.workdir / "phase-g-question.txt"
    if artifact_path.exists():
        artifact_path.unlink()
    task_id = client.submit(
        "Before making any workspace change, ask me one question using your user-input mechanism "
        "and wait for my answer.\n\nAsk: Which marker should I write?\n\nOffer exactly two "
        "choices: alpha, beta.\n\nAfter I answer, create phase-g-question.txt containing exactly "
        "the selected marker. Do not modify any other file.",
        runtime=RUNTIME,
    )
    client.wait_for(task_id, {"waiting_for_question"}, timeout=420)
    request_id, nested = client.pending_request(task_id)
    kind = nested.get("kind")
    payload = nested.get("payload")
    questions = payload.get("questions") if isinstance(payload, dict) else None
    selection = _select_option(questions, "alpha")
    if kind != "question" or selection is None:
        emit(
            "question",
            status="invalid_pending_request",
            pending_kind=kind,
            selection_found=selection is not None,
        )
        return gate_record(False, status="invalid_pending_request", pending_kind=kind)
    question_id, option_id = selection
    client.call(
        "answer_agent_question",
        {
            "task_id": task_id,
            "request_id": request_id,
            "answers": [{"question_id": question_id, "selected_option_ids": [option_id]}],
        },
    )
    status = client.wait_status(task_id, timeout=TURN_TIMEOUT_S, allow_approval=True)
    events = client.event_types(task_id)
    content = artifact_path.read_text(encoding="utf-8").strip() if artifact_path.exists() else None
    requested = any("question.requested" in n for n in events)
    answered = any("question.answered" in n for n in events)
    passed = status == "succeeded" and requested and answered and content == "alpha"
    emit(
        "question",
        status=status,
        question_requested=requested,
        question_answered=answered,
        artifact_is_alpha=content == "alpha",
    )
    return gate_record(
        passed,
        status=status,
        question_requested=requested,
        question_answered=answered,
        artifact_is_alpha=content == "alpha",
    )


def _select_option(questions: Any, label: str) -> tuple[str, str] | None:
    if not isinstance(questions, list):
        return None
    for question in questions:
        if not isinstance(question, dict):
            continue
        for option in question.get("options", []):
            if isinstance(option, dict) and str(option.get("label", "")).lower() == label:
                return str(question.get("question_id")), str(option.get("option_id"))
    return None


def _wait_started_with_children(
    client: McpStdioClient,
    lifecycle: Lifecycle,
    task_id: str,
    started: Path,
    baseline: list[int],
    *,
    budget_s: float = 420.0,
) -> tuple[bool, list[int], str, list[str]]:
    """Wait until the marker exists, answering approvals inline, then diff the claude children.

    Returns (started_seen, new_child_pids, status_before_cancel, approvals_answered). The child
    diff is what makes the later interrupt evidence attributable to this chain even if the
    operator runs their own Claude elsewhere.
    """
    answered: list[str] = []
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        task = client.task(task_id)
        status = task["status"]
        if status in TERMINAL:
            return False, [], status, answered
        if status == "waiting_for_approval":
            request_id, nested = client.pending_request(task_id)
            if request_id not in answered and "approve_once" in client.offered_decisions(nested):
                client.call(
                    "respond_agent_approval",
                    {"task_id": task_id, "request_id": request_id, "decision": "approve_once"},
                )
                answered.append(request_id)
        if started.exists():
            # No sleep-for-readiness anywhere: the marker *is* the readiness signal, and the
            # claude child has been alive since the SDK connected, so the diff is safe to take
            # the moment the marker is observed.
            now = _claude_child_pids()
            return True, [pid for pid in now if pid not in baseline], status, answered
        time.sleep(0.5)
    return False, [], client.task(task_id)["status"], answered


def _interrupt(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """The interrupt half: the *provider activity* stopped when the SDK interrupt ran.

    Evidence is deliberately about the provider, not the task record: the claude child that was
    observed mid-turn disappears within a bounded window, and the completion marker the command
    would have written after its sleep never appears. Whether the ServerFS task record ended in
    the right terminal state is the cancellation gate's claim, not this one's.
    """
    started = lifecycle.workdir / INTERRUPT_STARTED
    completion = lifecycle.workdir / INTERRUPT_COMPLETION
    for path in (started, completion):
        if path.exists():
            path.unlink()
    baseline = _claude_child_pids()
    emit("interrupt_stage", phase="baseline", claude_child_baseline_count=len(baseline))

    task_id = client.submit(
        "Run this exact shell command with the Bash tool and do nothing else:\n\n"
        f"python -c \"import time; open('{INTERRUPT_STARTED}','w').write('started'); "
        f"time.sleep(120); open('{INTERRUPT_COMPLETION}','w').write('done')\"\n\n"
        "Do not finish early and do not create any other file.",
        runtime=RUNTIME,
    )
    seen, new_children, status_before, answered = _wait_started_with_children(
        client, lifecycle, task_id, started, baseline
    )
    emit(
        "interrupt_stage",
        phase="activity_observed",
        started_artifact_seen=seen,
        new_claude_children=len(new_children),
        status_before=status_before,
        approvals_answered=len(answered),
    )
    if not seen or status_before in TERMINAL:
        return gate_record(
            False,
            started_artifact_seen=seen,
            status_before=status_before,
            reason="no mid-turn activity to interrupt",
        )

    client.call("cancel_agent_task", {"task_id": task_id})
    final = client.wait_status(task_id, timeout=300, allow_approval=True)
    child_gone, survivors = _wait_child_gone(new_children, CHILD_EXIT_WINDOW_S)
    completion_absent = not completion.exists()
    passed = child_gone and completion_absent
    emit(
        "interrupt",
        final_status=final,
        claude_child_gone=child_gone,
        claude_child_survivors=len(survivors),
        completion_artifact_absent=completion_absent,
    )
    return gate_record(
        passed,
        final_status=final,
        claude_child_gone=child_gone,
        claude_child_survivors=len(survivors),
        completion_artifact_absent=completion_absent,
        task_id=task_id,
    )


def _cancellation(
    client: McpStdioClient, lifecycle: Lifecycle, interrupt_task_id: str | None
) -> dict[str, Any]:
    """The cancellation half: the *ServerFS task semantics* held after the same cancel.

    Evidence is deliberately about the task layer: a terminal ``cancelled`` status on the public
    projection, the task present in this chain's own store, and a released writer lease proven by
    a follow-up task succeeding. None of that is derived from the provider child's fate.
    """
    if interrupt_task_id is None:
        return gate_record(False, reason="no interrupted task to inspect")
    public = client.task(interrupt_task_id)
    final_status = public.get("status")
    in_own_store = task_in_store(lifecycle, interrupt_task_id)

    follow_status = None
    follow_error = None
    follow_delay_s = None
    settle_deadline = time.monotonic() + 30
    while True:
        try:
            follow = client.submit(
                f"Create {AFTER_CANCEL} containing exactly:\n\nstill-alive\n\n"
                "Do not modify any other file.",
                runtime=RUNTIME,
            )
            follow_status = client.wait_status(follow, timeout=TURN_TIMEOUT_S, allow_approval=True)
            break
        except Exception as exc:  # noqa: BLE001 - the failure class is the result
            if "WORKDIR_BUSY" not in str(exc) or time.monotonic() >= settle_deadline:
                follow_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                break
            follow_delay_s = round(30 - (settle_deadline - time.monotonic()), 1)
            time.sleep(1.0)

    passed = final_status == "cancelled" and in_own_store is True and follow_status == "succeeded"
    emit(
        "cancellation",
        final_status=final_status,
        task_in_own_store=in_own_store,
        follow_up_status=follow_status,
        follow_up_error=follow_error,
        lease_release_delay_s=follow_delay_s,
    )
    return gate_record(
        passed,
        final_status=final_status,
        task_in_own_store=in_own_store,
        follow_up_status=follow_status,
        follow_up_error=follow_error,
        lease_release_delay_s=follow_delay_s,
    )


# -- Layer 3 gates --------------------------------------------------------------------------------


def _recovery(client: McpStdioClient, lifecycle: Lifecycle) -> dict[str, Any]:
    """Recovery classification after an abrupt crash, then honest resumability.

    The containment half (Job reaps Bridge and claude child; bystander survives) is the
    supervisor's claim. The adapter half must remain ``SESSION_RESUMABLE`` with
    ``provider_active=None`` semantics -- proven here by behaviour: after the restart the task is
    classified interrupted-and-resumable, the guard no longer blocks mutations, and a
    continuation succeeds with the **same native session** and produces its **own** native turn
    id (the pre-crash turn never completed, so no pre-crash turn id exists to compare against).
    None of that is worded as in-flight reattachment: the pre-crash artifact the command would
    have written never appears.
    """
    started = lifecycle.workdir / RECOVERY_STARTED
    completion = lifecycle.workdir / RECOVERY_COMPLETION
    after = lifecycle.workdir / RECOVERY_AFTER
    for path in (started, completion, after):
        if path.exists():
            path.unlink()

    baseline_children = _claude_child_pids()
    crash_task_id = client.submit(
        "Run this exact shell command with the Bash tool and do nothing else:\n\n"
        f"python -c \"import time; open('{RECOVERY_STARTED}','w').write('started'); "
        f"time.sleep(120); open('{RECOVERY_COMPLETION}','w').write('done')\"\n\n"
        "Do not finish early and do not create any other file.",
        runtime=RUNTIME,
    )
    seen, new_children, status_before, answered = _wait_started_with_children(
        client, lifecycle, crash_task_id, started, baseline_children
    )
    emit(
        "recovery_stage",
        phase="activity_observed",
        started_artifact_seen=seen,
        new_claude_children=len(new_children),
        status_before=status_before,
        approvals_answered=len(answered),
    )
    if not seen:
        return gate_record(False, reason="no mid-turn activity before the crash")
    lease_blocked = not _mutation_allowed(client)
    emit("recovery_stage", phase="pre_crash", writer_lease_blocks_mutation=lease_blocked)
    if not lease_blocked:
        return gate_record(False, reason="writer lease did not block a mutation pre-crash")

    # The recovery under test is "a resumable session got crashed", so the session identity must
    # already be durable before the crash is delivered. The provider announces it in the init
    # system message and the adapter persists it mid-turn; bounded polling observes exactly that,
    # and the native *turn* id is deliberately not awaited -- an in-flight turn has none, since
    # only the completing ResultMessage carries one. (Its absence is recorded as observed, not
    # demanded: a provider that surfaces a turn id earlier would not be wrong.)
    session_durable_deadline = time.monotonic() + 30
    session_before = None
    turn_before = None
    while time.monotonic() < session_durable_deadline:
        session_before, turn_before, _status = _read_native(lifecycle, crash_task_id)
        if session_before:
            break
        time.sleep(0.5)
    if not session_before:
        return gate_record(
            False,
            reason="native session was never durable before the crash",
            clause="SESSION_NOT_DURABLE",
        )
    emit(
        "recovery_stage",
        phase="session_durable",
        session_durable=True,
        pre_crash_turn_id_present=bool(turn_before),
    )

    bridge_pids = lifecycle.bridge_pids()
    bystander = _start_bystander()

    killed = _terminate_abruptly(lifecycle)
    bridge_gone, bridge_survivors = _wait_gone(bridge_pids, CONTAINMENT_WAIT_S)
    child_gone, child_survivors = _wait_gone(new_children, CONTAINMENT_WAIT_S)
    bystander_alive = bystander.poll() is None
    completion_absent = not completion.exists()
    emit(
        "containment",
        bridge_killed=bridge_gone,
        bridge_survivors=len(bridge_survivors),
        claude_child_killed=child_gone,
        claude_child_survivors=len(child_survivors),
        bystander_alive=bystander_alive,
        killed_pids=len(killed),
    )
    if not (bridge_gone and child_gone and bystander_alive):
        return gate_record(
            False,
            reason="containment failed",
            bridge_gone=bridge_gone,
            claude_child_gone=child_gone,
            bystander_alive=bystander_alive,
        )

    # Nothing is cleaned by hand: the store, lease artifacts and guard are what
    # reconciliation reads.
    restart = Lifecycle(
        lifecycle.tmp_path,
        env_file=lifecycle.env_file,
        codex_home=lifecycle.codex_home,
        use_proxy=lifecycle.use_proxy,
        read_only=lifecycle.read_only,
        runtime=RUNTIME,
    )
    require_file_stderr(restart)
    restart.launch()
    restart_client = McpStdioClient(restart)
    restart_client.initialize()

    session_after, turn_after, status_after = _read_native(restart, crash_task_id)
    mutation_ok = _mutation_allowed(restart_client)
    emit(
        "reconciliation",
        status_before=status_before,
        status_after=status_after,
        native_session_preserved=bool(session_after) and session_after == session_before,
        guard_released=mutation_ok,
    )

    # The two recovery facts are pinned separately, from the public event surface, because they
    # are made by different layers and neither may be inferred from the other:
    #
    # - ``runtime.reconcile_finished`` is the Claude adapter's own classification:
    #   SESSION_RESUMABLE with provider_active left unknown (None) -- the adapter cannot know
    #   whether a pre-crash process is still running.
    # - ``task.reconciled`` (the BRIDGE_RESTARTED one) is the service/startup layer combining
    #   that answer with the Windows containment proof, which is the only path allowed to turn
    #   the unknown into effective provider_active=false.
    events = restart_client.events(crash_task_id)
    adapter_finished = next(
        (e for e in events if str(e.get("event_type")) == "runtime.reconcile_finished"),
        None,
    )
    adapter_payload = (
        adapter_finished.get("payload") if isinstance(adapter_finished, dict) else None
    )
    adapter_classification_session_resumable = bool(
        isinstance(adapter_payload, dict)
        and adapter_payload.get("status") == "SESSION_RESUMABLE"
        and adapter_payload.get("provider_active") is None
    )
    task_reconciled = next(
        (
            e
            for e in events
            if str(e.get("event_type")) == "task.reconciled"
            and isinstance(e.get("payload"), dict)
            and e["payload"].get("error_code") == "BRIDGE_RESTARTED"
        ),
        None,
    )
    reconciled_payload = (
        task_reconciled.get("payload") if isinstance(task_reconciled, dict) else None
    )
    effective_provider_inactive = bool(
        isinstance(reconciled_payload, dict)
        and reconciled_payload.get("status") == "SESSION_RESUMABLE"
        and reconciled_payload.get("provider_active") is False
    )
    emit(
        "recovery_classification",
        adapter_classification_session_resumable=adapter_classification_session_resumable,
        adapter_provider_active_unknown=adapter_payload.get("provider_active") is None
        if isinstance(adapter_payload, dict)
        else False,
        effective_provider_inactive=effective_provider_inactive,
        reconcile_event_seen=adapter_finished is not None,
        bridged_reconcile_event_seen=task_reconciled is not None,
    )

    cont_id = restart_client.submit(
        f"Create {RECOVERY_AFTER} containing exactly:\n\nresumed-after-restart\n\n"
        "Do not modify any other file.",
        runtime=RUNTIME,
        continue_from=crash_task_id,
    )
    cont_status = restart_client.wait_status(cont_id, timeout=TURN_TIMEOUT_S, allow_approval=True)
    body = restart_client.read_file(RECOVERY_AFTER)
    cont_session, cont_turn, _ = _read_native(restart, cont_id)
    # Same native session as the crashed task; the continuation produced its *own* native turn
    # id. The pre-crash turn id is absent by construction (an interrupted turn never completes),
    # so "turn ids differ" is not a claim this evidence can make.
    same_session = bool(cont_session) and cont_session == session_before
    continuation_turn_present = bool(cont_turn)
    try:
        restart.stop()
    except Exception:  # noqa: BLE001
        restart.kill()

    passed = (
        status_after == "interrupted"
        and mutation_ok
        and adapter_classification_session_resumable
        and effective_provider_inactive
        and cont_status == "succeeded"
        and (body or "").strip() == "resumed-after-restart"
        and same_session
        and continuation_turn_present
        and completion_absent
    )
    emit(
        "recovery",
        reconciled_status=status_after,
        status_after=status_after,
        guard_released=mutation_ok,
        adapter_classification_session_resumable=adapter_classification_session_resumable,
        effective_provider_inactive=effective_provider_inactive,
        continuation_status=cont_status,
        continuation_artifact_exact=(body or "").strip() == "resumed-after-restart",
        same_native_session=same_session,
        continuation_native_turn_present=continuation_turn_present,
        in_flight_artifact_absent=completion_absent,
    )
    return gate_record(
        passed,
        status_after=status_after,
        guard_released=mutation_ok,
        adapter_classification_session_resumable=adapter_classification_session_resumable,
        effective_provider_inactive=effective_provider_inactive,
        continuation_status=cont_status,
        same_native_session=same_session,
        continuation_native_turn_present=continuation_turn_present,
        in_flight_artifact_absent=completion_absent,
    )


# -- Phase runners --------------------------------------------------------------------------------


def _chain(tmp_root: Path, *, use_proxy: bool, extra_child_env: dict[str, str] | None = None):
    lifecycle = Lifecycle(
        tmp_root,
        env_file=REPO_ROOT / ".env",
        codex_home=Path.home() / ".codex",
        use_proxy=use_proxy,
        read_only=False,
        runtime=RUNTIME,
        extra_child_env=extra_child_env,
    )
    require_file_stderr(lifecycle)
    return lifecycle


def run_phase_1() -> int:
    results: dict[str, Any] = {}
    pre = claude_preflight()
    if not pre["ok"]:
        emit("verdict", answer="G2_PHASE1_PARTIAL", failed=["claude_preflight"], **pre)
        return 5

    tmp_root = Path(tempfile.mkdtemp(prefix="phase-g-layer1-"))
    lifecycle = _chain(tmp_root, use_proxy=False)
    client = McpStdioClient(lifecycle)
    try:
        lifecycle.launch()
        results["ownership"] = gate_record(True, **require_own_bridge(lifecycle))
        client.initialize()
        results["probe"] = _run_gate("probe", lambda: _probe(client))
        if not results["probe"]["passed"]:
            raise RuntimeError("probe gate failed; a direct turn cannot be accepted after it")
        results["direct_turn"] = _run_gate("direct_turn", lambda: _direct_turn(client, lifecycle))
        task_id = results["direct_turn"].get("task_id")
        if task_id:
            results["session_creation"] = _run_gate(
                "session_creation", lambda: _session_creation(client, lifecycle, task_id)
            )
    except (ToolError, RuntimeError, TimeoutError) as exc:
        emit("run_failed", error_class=type(exc).__name__, detail=str(exc)[:300])
        results["run_failed"] = {"error_class": type(exc).__name__, "detail": str(exc)[:300]}
    finally:
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            lifecycle.kill()
        children = _claude_child_pids()
        results["direct_cleanup"] = gate_record(
            not lifecycle.bridge_pids() and not children,
            bridge_stopped=not lifecycle.bridge_pids(),
            claude_children_remaining=len(children),
        )
        emit("direct_cleanup", **results["direct_cleanup"])
        _dispose(tmp_root, results, PHASE_GATES[1], ("direct_cleanup",))

    emit("results", **results)
    verdict = _verdict(results, 1)
    emit("verdict", **verdict)
    return 0 if verdict["answer"] == "G2_PHASE1_PASS" else 5


def run_phase_2() -> int:
    results: dict[str, Any] = {}
    forwarder = ConnectForwarder()
    forwarder.start()
    emit("forwarder", listening=True, port_exposed=False)

    tmp_root = Path(tempfile.mkdtemp(prefix="phase-g-layer2-"))
    # The dedicated endpoint the supervisor reads is overridden to this run's own credentialless
    # forwarder, applied before the tree is spawned (Lifecycle folds extra_child_env in last).
    lifecycle = _chain(
        tmp_root,
        use_proxy=True,
        extra_child_env={"SERVERFS_AGENT_PROXY_URL": f"http://127.0.0.1:{forwarder.port}"},
    )
    client = McpStdioClient(lifecycle)
    try:
        lifecycle.launch()
        results["proxy_ownership"] = gate_record(True, **require_own_bridge(lifecycle))
        client.initialize()
        results["proxy_turn"] = _run_gate(
            "proxy_turn", lambda: _proxy_turn(client, lifecycle, forwarder)
        )
    except (ToolError, RuntimeError, TimeoutError) as exc:
        emit("run_failed", error_class=type(exc).__name__, detail=str(exc)[:300])
        results["run_failed"] = {"error_class": type(exc).__name__, "detail": str(exc)[:300]}
    finally:
        try:
            lifecycle.stop()
        except Exception:  # noqa: BLE001
            lifecycle.kill()
        children = _claude_child_pids()
        results["proxy_cleanup"] = gate_record(
            not lifecycle.bridge_pids() and not children,
            bridge_stopped=not lifecycle.bridge_pids(),
            claude_children_remaining=len(children),
        )
        emit("proxy_cleanup", **results["proxy_cleanup"])
        forwarder.stop()
        emit("forwarder_summary", **forwarder.summary())
        _dispose(tmp_root, results, PHASE_GATES[2], ("proxy_cleanup",))

    emit("results", **results)
    verdict = _verdict(results, 2)
    emit("verdict", **verdict)
    return 0 if verdict["answer"] == "G2_PHASE2_PASS" else 5


def run_phase_3() -> int:
    results: dict[str, Any] = {}
    tmp_root = Path(tempfile.mkdtemp(prefix="phase-g-layer3-"))
    lifecycle = _chain(tmp_root, use_proxy=False)
    client = McpStdioClient(lifecycle)
    interrupt_task_id: str | None = None
    try:
        lifecycle.launch()
        client.initialize()

        results["file_mutation"] = _run_gate(
            "file_mutation", lambda: _file_mutation(client, lifecycle)
        )
        source_task_id = results["file_mutation"].get("task_id")
        if source_task_id:
            results["continuation"] = _run_gate(
                "continuation", lambda: _continuation(client, lifecycle, source_task_id)
            )
        results["approval"] = _run_gate("approval", lambda: _approval(client))
        results["question"] = _run_gate("question", lambda: _question(client, lifecycle))
        interrupt_gate = _run_gate("interrupt", lambda: _interrupt(client, lifecycle))
        results["interrupt"] = interrupt_gate
        interrupt_task_id = interrupt_gate.get("task_id")
        results["cancellation"] = _run_gate(
            "cancellation", lambda: _cancellation(client, lifecycle, interrupt_task_id)
        )
    except (ToolError, RuntimeError, TimeoutError) as exc:
        emit("run_failed", error_class=type(exc).__name__, detail=str(exc)[:300])
        results["run_failed"] = {"error_class": type(exc).__name__, "detail": str(exc)[:300]}

    # Time isolation is the product's own contract: the Agent endpoint is derived from the user
    # SID alone, so a second Agent-enabled chain of this user cannot start while this Bridge
    # lives. The recovery chain therefore launches only after this chain has stopped and its
    # Bridge is confirmed gone -- measured in the first Phase 3 run, which died at
    # initialize() on exactly this refusal.
    try:
        lifecycle.stop()
    except Exception:  # noqa: BLE001
        lifecycle.kill()
    main_chain_bridge_gone = not lifecycle.bridge_pids()
    emit("main_chain_stopped", bridge_gone=main_chain_bridge_gone)

    # Layer 3's recovery half runs on its own chain: the crash under test must not take the
    # earlier gates' evidence with it.
    recovery_root: Path | None = None
    try:
        recovery_root = Path(tempfile.mkdtemp(prefix="phase-g-recovery-"))
        recovery_chain = _chain(recovery_root, use_proxy=False)
        require_file_stderr(recovery_chain)
        recovery_client = McpStdioClient(recovery_chain)
        recovery_chain.launch()
        recovery_client.initialize()
        results["recovery"] = _run_gate(
            "recovery", lambda: _recovery(recovery_client, recovery_chain)
        )
    except (ToolError, RuntimeError, TimeoutError) as exc:
        emit("recovery_run_failed", error_class=type(exc).__name__, detail=str(exc)[:300])
        results["recovery"] = gate_record(
            False, error_class=type(exc).__name__, detail=str(exc)[:300]
        )
    finally:
        if recovery_root is not None:
            if (
                isinstance(results.get("recovery"), dict)
                and results["recovery"].get("passed") is True
            ):
                shutil.rmtree(recovery_root, ignore_errors=True)
            else:
                emit("scene_preserved", path=str(recovery_root))

    children = _claude_child_pids()
    residual_bridges = lifecycle.bridge_pids()
    results["cleanup"] = gate_record(
        main_chain_bridge_gone and not residual_bridges and not children,
        main_chain_bridge_gone=main_chain_bridge_gone,
        bridge_stopped=not residual_bridges,
        claude_children_remaining=len(children),
    )
    emit("cleanup", **results["cleanup"])

    _dispose(tmp_root, results, PHASE_GATES[3], ("cleanup",))

    emit("results", **results)
    verdict = _verdict(results, 3)
    emit("verdict", **verdict)
    return 0 if verdict["answer"] == "G2_PHASE3_PASS" else 5


def _dispose(
    tmp_root: Path, results: dict[str, Any], gates: tuple[str, ...], keep: tuple[str, ...]
) -> None:
    """Remove the scene only when every substantive gate passed; a failed run keeps its evidence.

    The first Phase G run destroyed its own diagnostics with an unconditional rmtree -- the same
    harness fault Phase E recorded and fixed. Failure scenes are preserved and their path printed
    instead.
    """
    substantive = [name for name in gates if name not in keep]
    ok = all(
        isinstance(results.get(name), dict) and results[name].get("passed") is True
        for name in substantive
    )
    if ok:
        shutil.rmtree(tmp_root, ignore_errors=True)
    else:
        emit("scene_preserved", path=str(tmp_root))


def main() -> int:
    parser = argparse.ArgumentParser(description="G2 real-provider acceptance (Claude)")
    parser.add_argument("--phase", type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()
    endpoint_present = bool(
        load_env_file(REPO_ROOT / ".env").get("SERVERFS_AGENT_PROXY_URL", "").strip()
    )
    emit("setup", runtime=RUNTIME, agent_proxy_env_present=endpoint_present, model="native-default")
    if args.phase == 1:
        return run_phase_1()
    if args.phase == 2:
        return run_phase_2()
    return run_phase_3()


if __name__ == "__main__":
    sys.exit(main())
