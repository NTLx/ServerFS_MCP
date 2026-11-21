"""Source-only bounded one-use I5 real Provider stream cancellation acceptance contracts.

Never invokes actual Claude, HTTP, Task Scheduler or credentials.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "deployment/windows"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PARENT = _ROOT / "v0250-i5-real-claude-token-stream-cancel-smoke.py"
_PROOF = _ROOT / "v0250-i5-real-stream-proof-worker.py"
parent = _load(_PARENT, "i5_genuine_token_stream_contract")
proof = _load(_PROOF, "i5_installed_token_stream_proof_contract")


def test_one_use_provider_gate_reuses_protected_lifecycle() -> None:
    source = _PARENT.read_text(encoding="utf-8")
    for token in (
        'sys.argv[1:] == ["--run-once"]',
        "old.run_cancel = run_delta_cancel",
        "old.prove_native_session_after_stop = both_proofs",
        "old.main()",
        'print("I5_REAL_PROVIDER_TOKEN_STREAM_CANCEL=PASS")',
        "time.monotonic() + 65",
        "I5_REAL_PROVIDER_TEXT_DELTA_RUNNING=PASS",
        "I5_REAL_STREAM_FOREIGN_CORE_FOUR_METHODS=DENIED",
        "I5_REAL_STREAM_OWNER_CANCELLED=PASS",
        "I5_REAL_STREAM_CANCEL_EVENT_ORDER=PASS",
        "I5_REAL_STREAM_FINALLY_CANCEL=UNVERIFIED",
    ):
        assert token in source
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflared tunnel",
        "Set-ScheduledTask",
        "git push",
        "approve_session",
        "PermissionResultAllow",
    ):
        assert forbidden not in source
    assert parent.TERMINAL == {"succeeded", "failed", "cancelled", "interrupted"}


def test_worker_proof_is_only_private_readonly_sqlite() -> None:
    source = _PROOF.read_text(encoding="utf-8")
    for token in (
        "mode=ro&immutable=1",
        "PRAGMA query_only=ON",
        "verify_private_file(",
        "prior.verify_cancelled_task(con, task_id)",
        "I5_REAL_STREAM_PRIVATE_TEXT_BEFORE_CANCEL=PASS",
        "max(deltas) < cancels[0]",
        "event_type IN ('agent.text_delta', 'task.cancelled')",
    ):
        assert token in source
    assert proof._ID.fullmatch("agt_" + "a" * 24) is not None
    assert proof._ID.fullmatch("agt_" + "g" * 24) is None
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "TaskStore(",
        "subprocess.Popen(",
        "git push",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source


def test_real_stream_gate_fails_closed_without_echoing_provider_content() -> None:
    with pytest.raises(parent.I5RealStreamError):
        parent.require(False, "not accepted")
    parent.require(True, "accepted")
    with pytest.raises(proof.I5PrivateStreamError):
        proof.require(False, "not accepted")
