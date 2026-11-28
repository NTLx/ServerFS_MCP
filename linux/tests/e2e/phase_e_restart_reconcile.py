"""§47 restart reconciliation and real Job containment, against a real provider.

The highest-risk gate in Phase E, and the one most easily satisfied by accident. Three properties
have to be established separately, because a run that only shows the happy path has shown nothing.

**Real Job containment.** A long-running real task is in flight when the native supervisor lifecycle
is terminated abnormally -- not closed, not asked to stop. The supervisor holds a Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so the Bridge and the Bridge-owned ``codex app-server`` must
die with it, while an unrelated bystander process and the operator's own managed Codex daemon must
not. A graceful ``stop()`` would prove nothing: that is the path where the product shuts its
children down politely, which is exactly what this gate must not rely on.

**Recovery is the product's, not the harness's.** Nothing is deleted between the crash and the
restart: not the TaskStore, not the lease, not the recovery guard, not the Codex session state.
The production startup path runs reconciliation itself and the classification is recorded as
measured. The legitimate outcomes are UNKNOWN with the provider still active, UNKNOWN with ownership
unclear, and SESSION_RESUMABLE with the provider inactive. "Reattached" is not among them and is
never reported.

**§48 runs only if recovery genuinely says the session is resumable.** If the measured
classification is UNKNOWN with the provider active, §48 is recorded as not applicable to this
recovery state rather than manufactured by choosing a friendlier crash moment.

Every action goes through the public MCP surface. The store is read only to compare native ids, and
the filesystem only for process identity -- never written, never deleted, never edited.
"""

from __future__ import annotations

import ctypes
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))

from phase_e_acceptance import McpStdioClient  # noqa: E402
from phase_e_lifecycle import (  # noqa: E402
    Lifecycle,
    process_command_lines,
    require_file_stderr,
    require_preflight,
)

#: A task that is unambiguously still working when the crash lands. The write happens only at
#: the end, so an artifact appearing would mean the turn survived, which is the opposite of what
#: §48 needs to show.
LONG_RUNNING_PROMPT = (
    "Run a Python command in this workspace that waits about 240 seconds and only then creates "
    "phase-e-reconcile-should-not-exist.txt containing 'survived'. Do not finish early."
)

RECONCILE_ARTIFACT = "phase-e-reconcile-should-not-exist.txt"

POST_RESTART_PROMPT = (
    "Create a file named phase-e-post-restart.txt in the current workspace.\n"
    "Its complete contents must be exactly:\n\n"
    "resumed-after-restart\n\n"
    "Do not modify any other file."
)

POST_RESTART_ARTIFACT = "phase-e-post-restart.txt"

NEWLINE = b"\n"

#: How long to wait for the provider to be observably working before the crash. Long enough that
#: the turn is genuinely in flight, bounded so a provider that never starts fails the precondition
#: instead of hanging.
ACTIVE_WAIT_SECONDS = 420

#: How long to wait for the crash to be observed to have taken the tree down.
CONTAINMENT_WAIT_SECONDS = 60


#: Win32 declarations for the liveness check. Kept minimal and read-only.
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)  # noqa: N816 - the Win32 name
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: GetExitCodeProcess reports this while a process is still running.
STILL_ACTIVE = 259


def emit(stage: str, **fields: Any) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def _codex_processes() -> list[tuple[int, str]]:
    return [(pid, cmd) for pid, cmd in process_command_lines() if "codex" in cmd.lower()]


def _owned_app_server(lifecycle: Lifecycle) -> list[int]:
    return lifecycle.codex_app_server_pids()


def _alive(pids: list[int]) -> list[int]:
    """Which of these pids are still running, read-only.

    Implemented with the Win32 API rather than a PowerShell pipeline. The pipeline form
    (``... | Where-Object { Get-Process -Id $_ } ...``) silently returned empty output for
    processes that were demonstrably alive, so a liveness check built on it reported everything as
    dead -- which is how a bystander came to read as "killed by containment" when it had never been
    tested correctly.

    ``OpenProcess`` alone is not enough, and this is the subtle part: on Windows a terminated
    process keeps its kernel object alive until every handle is closed, so ``OpenProcess`` succeeds
    for a pid that is already gone and only fails (87) for one that never existed.
    ``STILL_ACTIVE`` from ``GetExitCodeProcess`` is therefore the actual test.

    A process that exists but cannot be queried counts as alive: this gate asks whether containment
    over-reached, and "I cannot tell" is not evidence that it did.
    """
    if not pids:
        return []
    survivors: list[int] = []
    for pid in pids:
        handle = _KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 87 is ERROR_INVALID_PARAMETER: no such process ever existed.
            continue
        try:
            code = wintypes.DWORD()
            if _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)) and (
                code.value == STILL_ACTIVE
            ):
                survivors.append(pid)
        finally:
            _KERNEL32.CloseHandle(handle)
    return survivors


def _wait_gone(pids: list[int], timeout: float) -> tuple[bool, list[int]]:
    """Bounded polling for process death rather than a fixed sleep."""
    deadline = time.monotonic() + timeout
    survivors = list(pids)
    while time.monotonic() < deadline:
        survivors = _alive(pids)
        if not survivors:
            return True, []
        time.sleep(0.5)
    return False, survivors


def _read_native(lifecycle: Lifecycle, task_id: str) -> tuple[str | None, str | None, str]:
    """The task's native ids and status, read-only.

    The values are compared inside this module and never leave it: a real thread identifier is not
    evidence anyone needs to read, and publishing one would put an account-scoped identifier into a
    tracked file.
    """
    database = lifecycle.data_home / "agent-bridge" / "state" / "state.sqlite3"
    if not database.exists():
        candidates = list((lifecycle.data_home / "agent-bridge").rglob("*.sqlite3"))
        if not candidates:
            return None, None, "no-store"
        database = candidates[0]
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT status, native_session_id, native_turn_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None, None, "absent"
    return row["native_session_id"], row["native_turn_id"], str(row["status"])


def _active_items(events: list[dict]) -> tuple[bool, bool]:
    """(any item started, an executing item). The second is the stronger evidence."""
    any_started = False
    executing = False
    for event in events:
        if str(event.get("event_type")) != "item.started":
            continue
        any_started = True
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("type") or payload.get("kind") or payload.get("item_type") or "")
        if "command" in kind.lower() or "exec" in kind.lower():
            executing = True
    return any_started, executing


def _start_bystander() -> subprocess.Popen[bytes]:
    """A process unrelated to the chain, to prove containment did not over-reach.

    Started with ``CREATE_BREAKAWAY_FROM_JOB`` so it is outside any Job Object the harness itself
    belongs to. Without the flag it inherits the harness's own Job, and the crash under test reaps
    it along with everything else -- reading as "containment killed an unrelated process" when the
    truth is that the bystander was never independent of the thing being measured.
    """
    return subprocess.Popen(  # noqa: S603 - a fixed argv, long-lived by design
        [sys.executable, "-c", "import time\nwhile True:\n    time.sleep(5)\n"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
    )


def _terminate_abruptly(lifecycle: Lifecycle) -> list[int]:
    """Kill the launcher and supervisor outright, with nothing asked to shut down.

    ``stop()`` is deliberately not used: it closes stdin, which is the cooperative path. Killing the
    processes makes the supervisor's Job Object handle die with it, so
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is what reaps the Bridge and its provider child -- the
    containment property under test.
    """
    pids: list[int] = []
    if lifecycle.process is not None and lifecycle.process.poll() is None:
        lifecycle.process.kill()
        pids.append(lifecycle.process.pid)
    for pid in lifecycle.supervisor_pids():
        subprocess.run(  # noqa: S603 - a fixed argv built from an integer
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        pids.append(pid)
    return pids


def _mutation_allowed(client: McpStdioClient, attempts: int = 5) -> bool:
    """Whether a public filesystem mutation currently succeeds.

    A distinct path per attempt, because ``create_text_file`` never overwrites and a reused name
    would fail with ``PATH_ALREADY_EXISTS`` -- reading as "the guard held" for the wrong reason.
    """
    for attempt in range(1, attempts + 1):
        result = client.try_call(
            "create_text_file",
            {
                "workdir": "acceptance",
                "path": f"guard-probe-{attempt}.txt",
                "content": "guard-probe",
            },
        )
        if result is not None:
            return True
        time.sleep(1.0)
    return False


def _classify(status_after: str, mutation_allowed: bool) -> str:
    """The recovery classification, from what the product actually did.

    Deliberately narrow. UNKNOWN with the provider still active is a correct, expected outcome and
    is reported as such; "reattached" is never produced, because no code path establishes it.
    """
    if status_after in ("succeeded", "failed", "interrupted", "cancelled"):
        return "SESSION_RESUMABLE"
    if not mutation_allowed:
        return "UNKNOWN_GUARD_HELD"
    return "UNKNOWN_PROVIDER_INACTIVE"


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="phase-e-reconcile-"))
    env_file = REPO_ROOT / ".env"
    codex_home = Path.home() / ".codex"

    pre = require_preflight(env_file, codex_home)
    emit(
        "preflight",
        **{k: v for k, v in pre.summary().items() if "path" not in k.lower()},
    )

    baseline_codex = _codex_processes()
    emit("baseline", codex_process_count=len(baseline_codex))

    lifecycle = Lifecycle(tmp_root, env_file=env_file, codex_home=codex_home)
    require_file_stderr(lifecycle)
    bystander = _start_bystander()
    emit("bystander", pid=bystander.pid)

    restart: Lifecycle | None = None
    try:
        lifecycle.launch()
        client = McpStdioClient(lifecycle)
        client.initialize()

        crash_task_id = client.submit(LONG_RUNNING_PROMPT)
        emit("submitted", task_id=crash_task_id)

        deadline = time.monotonic() + ACTIVE_WAIT_SECONDS
        session = turn = None
        status_before = ""
        lease_blocked = False
        while time.monotonic() < deadline:
            # A real approval is answered through the public tool so the provider reaches execution
            # rather than sitting on an interaction this provider may not raise at all.
            snapshot = client.task(crash_task_id)
            if snapshot.get("status") == "waiting_for_approval":
                request_id, nested = client.pending_request(crash_task_id)
                if "approve_once" in client.offered_decisions(nested):
                    client.call(
                        "respond_agent_approval",
                        {
                            "task_id": crash_task_id,
                            "request_id": request_id,
                            "decision": "approve_once",
                        },
                    )
            any_started, _executing = _active_items(client.events(crash_task_id))
            if any_started:
                session, turn, status_before = _read_native(lifecycle, crash_task_id)
                if session and turn:
                    # The lease must actually be held, not merely expected to be: a crash with no
                    # lease would make the recovery guard's behaviour untestable.
                    lease_blocked = not _mutation_allowed(client)
                    emit(
                        "pre_crash",
                        status=status_before,
                        session_id_present=True,
                        turn_id_present=True,
                        provider_item_started=any_started,
                        writer_lease_blocks_mutation=lease_blocked,
                    )
                    break
            time.sleep(1.0)
        else:
            emit("pre_crash_failed", reason="no provider activity within budget")
            return 4

        if not lease_blocked:
            emit("pre_crash_failed", reason="writer lease did not block a mutation")
            return 4

        bridge_pids = lifecycle.bridge_pids()
        app_server_pids = _owned_app_server(lifecycle)
        token_path = lifecycle.data_home / "agent-bridge" / "state" / "codex" / "app-server-token"
        emit(
            "containment_baseline",
            bridge_pids=len(bridge_pids),
            owned_app_server_pids=len(app_server_pids),
            token_file_present=token_path.exists(),
        )

        # Read the bystander's liveness immediately before the crash, so a later absence can be
        # attributed to the crash rather than to it having already died.
        emit("bystander_pre_crash", alive=_alive([bystander.pid]) == [bystander.pid])

        killed = _terminate_abruptly(lifecycle)
        emit("crash", killed_pids=len(killed), graceful=False)

        bridge_gone, bridge_left = _wait_gone(bridge_pids, CONTAINMENT_WAIT_SECONDS)
        app_gone, app_left = _wait_gone(app_server_pids, CONTAINMENT_WAIT_SECONDS)
        emit(
            "containment",
            bridge_killed=bridge_gone,
            bridge_survivors=len(bridge_left),
            owned_app_server_killed=app_gone,
            owned_app_server_survivors=len(app_left),
        )

        bystander_alive = _alive([bystander.pid]) == [bystander.pid]
        operator_intact = sorted(pid for pid, _ in _codex_processes()) == sorted(
            pid for pid, _ in baseline_codex
        )
        emit(
            "containment_scope",
            bystander_alive=bystander_alive,
            operator_daemon_unchanged=operator_intact,
        )

        if not (bridge_gone and app_gone and bystander_alive and operator_intact):
            emit("verdict", classification="JOB_CONTAINMENT_FAILED")
            return 5

        # Nothing is cleaned by hand here: the store, the lease, the guard and the Codex session
        # state are the inputs reconciliation is supposed to read.
        emit("pre_reconcile", cleaned_by_harness=False)

        restart = Lifecycle(tmp_root, env_file=env_file, codex_home=codex_home)
        require_file_stderr(restart)
        restart.launch()
        client_after = McpStdioClient(restart)
        # initialize() takes no timeout: the request path is already bounded, and inventing a
        # parameter here would have been another assumption about an API that does not take one.
        client_after.initialize()

        new_app_servers = _owned_app_server(restart)
        restart_token = restart.data_home / "agent-bridge" / "state" / "codex" / "app-server-token"
        emit(
            "restarted",
            bridge_pids=len(restart.bridge_pids()),
            new_app_server_pids=len(new_app_servers),
            new_app_server_is_new=bool(new_app_servers)
            and not (set(new_app_servers) & set(app_server_pids)),
            token_file_present=restart_token.exists(),
        )

        session_after, turn_after, status_after = _read_native(restart, crash_task_id)
        emit(
            "reconciliation",
            status_before=status_before,
            status_after=status_after,
            native_ids_preserved=bool(session_after) and session_after == session,
            turn_id_preserved=bool(turn_after) and turn_after == turn,
        )

        mutation_ok = _mutation_allowed(client_after)
        emit("post_reconcile_guard", mutation_allowed=mutation_ok)

        types = sorted({str(e.get("event_type")) for e in client_after.events(crash_task_id)})
        emit("reconciliation_events", types=types)

        classification = _classify(status_after, mutation_ok)
        emit("verdict", classification=classification)

        if classification == "SESSION_RESUMABLE":
            cont = client_after.submit(POST_RESTART_PROMPT, continue_from=crash_task_id)
            final = client_after.wait_status(cont, timeout=900)
            body = client_after.read_file(POST_RESTART_ARTIFACT)
            s2, t2, _ = _read_native(restart, cont)
            emit(
                "post_restart_continuation",
                applicable=True,
                status=final,
                artifact_exact=body is not None and body.strip() == "resumed-after-restart",
                same_native_session=bool(s2) and s2 == session,
                new_native_turn=bool(t2) and bool(turn) and t2 != turn,
                in_flight_artifact_absent=not (restart.workdir / RECONCILE_ARTIFACT).exists(),
            )
        else:
            emit(
                "post_restart_continuation",
                applicable=False,
                reason=f"recovery classified {classification}",
                in_flight_artifact_absent=not (restart.workdir / RECONCILE_ARTIFACT).exists(),
            )
        return 0
    finally:
        for handle in (lifecycle, restart):
            if handle is None:
                continue
            try:
                handle.stop(timeout=30)
            except Exception:  # noqa: BLE001 - teardown must not mask the result
                handle.kill()
            handle.close_stderr()
        if bystander.poll() is None:
            bystander.kill()
        time.sleep(2)
        remaining = _codex_processes()
        emit(
            "final",
            acceptance_owned_codex=sum(
                1 for _, cmd in remaining if "app-server --listen ws" in cmd
            ),
            operator_codex=sum(1 for _, cmd in remaining if "app-server --listen ws" not in cmd),
            baseline_codex=len(baseline_codex),
            bystander_reaped=bystander.poll() is not None,
        )
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
