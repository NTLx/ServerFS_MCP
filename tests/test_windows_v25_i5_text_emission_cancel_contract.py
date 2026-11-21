"""Source and SQLite contracts for the I5 genuine first-text-emission cancel."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "deployment/windows"
PARENT = ROOT / "v0250-i5-real-claude-text-emission-cancel-smoke.py"
PROOF = ROOT / "v0250-i5-stream-cancel-proof-worker.py"
SPEC = importlib.util.spec_from_file_location("i5_text_emission_proof_contract", PROOF)
assert SPEC is not None and SPEC.loader is not None
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


def test_provider_gate_requires_emitted_text_and_is_one_use() -> None:
    source = PARENT.read_text(encoding="utf-8")
    for marker in (
        'CORES = {"i5-core-a": 18873, "i5-core-b": 18874}',
        '"--run-once"',
        "smoke.candidates_disabled(gate)",
        "smoke.protected_snapshot(gate)",
        '"task.started"',
        '"agent.text_delta"',
        "len(deltas) <= 1",
        '{"size_bytes", "index"}',
        "provider-ended-before-text-delta",
        "real-provider-text-delta-not-observed",
        "I5_STREAM_CANCEL_REAL_TEXT_DELTA=PASS",
        "I5_REAL_PROVIDER_TEXT_EMISSION_CANCEL=PASS",
        "v0250-i5-stream-cancel-proof-worker.py",
        "reverse-stop-three-i5-fixtures",
    ):
        assert marker in source
    assert source.count('"submit_agent_task",') == 1
    for unsafe in ("cloudflared tunnel", "git push", "subprocess.Popen(", "enable_task("):
        assert unsafe not in source
    worker = PROOF.read_text(encoding="utf-8")
    assert "I5_STREAM_PRIVATE_SDK_TEXT_AND_DURABLE_OWNER=PASS" in worker
    assert "SELECT payload_json FROM events" in worker
    assert 'set(payload) == {"index", "size_bytes"}' in worker


def _memory_db() -> tuple[sqlite3.Connection, str]:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT, workdir_alias TEXT, runtime TEXT,
            status TEXT, native_session_id TEXT, final_response TEXT
        );
        CREATE TABLE core_task_owners (task_id TEXT, core_id TEXT);
        CREATE TABLE events (
            event_id INTEGER PRIMARY KEY, task_id TEXT,
            event_type TEXT, payload_json TEXT
        );
        CREATE TABLE pending_requests (task_id TEXT, status TEXT);
        """
    )
    task_id = "agt_" + "a" * 24
    con.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, "Fixture", "claude", "cancelled", "sdk-session", None),
    )
    con.execute("INSERT INTO core_task_owners VALUES (?, ?)", (task_id, "i5-core-a"))
    for number, kind, payload in (
        (1, "task.started", {}),
        (2, "agent.text_delta", {"size_bytes": 14, "index": 0}),
        (3, "task.cancelled", {}),
    ):
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            (number, task_id, kind, json.dumps(payload)),
        )
    return con, task_id


def test_private_proof_accepts_content_free_emission_then_cancel() -> None:
    con, task_id = _memory_db()
    try:
        proof.verify_cancelled_task(con, task_id)
        con.execute(
            "UPDATE events SET payload_json=? WHERE event_type='agent.text_delta'",
            (json.dumps({"text": "private path fragment", "index": 0}),),
        )
        with pytest.raises(proof.I5PrivateProofError, match="text-delta"):
            proof.verify_cancelled_task(con, task_id)
    finally:
        con.close()


def test_private_proof_rejects_missing_text_emission() -> None:
    con, task_id = _memory_db()
    try:
        con.execute("DELETE FROM events WHERE event_type='agent.text_delta'")
        with pytest.raises(proof.I5PrivateProofError, match="text"):
            proof.verify_cancelled_task(con, task_id)
    finally:
        con.close()
