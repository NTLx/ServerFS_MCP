from __future__ import annotations

import asyncio
import json

import pytest

from e2e.macos_shared_qoder_diagnostics import StageEvidence, safe_sdk_error_class


def test_shared_qoder_stage_evidence_redacts_unrecognized_failure(tmp_path):
    path = tmp_path / "stage-evidence.json"
    evidence = StageEvidence(path)
    evidence.record("fixture_started")
    evidence.record(
        "task_submitted",
        error_code="private token and path",
        error_class="PrivateProviderException",
    )

    text = path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["stages"] == [
        {"stage": "fixture_started"},
        {"stage": "task_submitted", "error_code": "OTHER", "error_class": "Exception"},
    ]
    assert record["runtime"] == "qoder"
    assert record["acceptance_base_sha"] == record["source_sha"]
    assert record["evidence_kind"] == "synthetic"
    assert record["source_sha"]
    assert record["test_file_sha256"] is None
    assert "private token and path" not in text
    assert "PrivateProviderException" not in text


def test_acceptance_provenance_refuses_mismatched_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVERFS_ACCEPTANCE_BASE_SHA", "a" * 40)
    with pytest.raises(ValueError, match="does not match"):
        StageEvidence(tmp_path / "mismatch.json", source_sha="b" * 40)
    evidence = StageEvidence(tmp_path / "match.json", source_sha="a" * 40)
    evidence.record("fixture_started")
    assert json.loads(evidence.path.read_text())["acceptance_base_sha"] == "a" * 40


def test_sdk_exception_class_never_retains_raw_messages():
    class PrivateProviderError(Exception):
        pass

    assert safe_sdk_error_class(TimeoutError("private path")) == "TimeoutError"
    assert safe_sdk_error_class(asyncio.CancelledError()) == "CancelledError"
    assert safe_sdk_error_class(PrivateProviderError("private token")) == "OTHER"


def test_shared_qoder_stage_evidence_rejects_arbitrary_stage(tmp_path):
    evidence = StageEvidence(tmp_path / "stage-evidence.json")
    try:
        evidence.record("/private/prompt")
    except ValueError as exc:
        assert str(exc) == "unsupported diagnostic stage"
    else:
        raise AssertionError("arbitrary stage was accepted")


def test_second_task_failure_does_not_reuse_first_terminal_stage(tmp_path):
    evidence = StageEvidence(tmp_path / "two-tasks.json", expected_tasks=2)
    for stage in (
        "fixture_started",
        "owner_starting",
        "owner_started",
        "core_staged",
        "core_ready",
        "task_submitted",
        "task_discovered",
        "task_terminal",
        "task_submitted",
        "task_discovered",
    ):
        evidence.record(stage)
    evidence.record("exception", error_class="TimeoutError")
    evidence.record("teardown_verified")
    evidence.record("finished")
    assert evidence.diagnose() == {
        "last_completed_stage": "task_discovered",
        "next_expected_stage": "task_terminal",
    }


def test_shared_qoder_stage_evidence_diagnoses_failure_without_terminal(tmp_path):
    evidence = StageEvidence(tmp_path / "stage-evidence.json")
    evidence.record("fixture_started")
    evidence.record("owner_starting")
    evidence.record("owner_started")
    evidence.record("core_staged")
    evidence.record("core_ready")
    evidence.record("task_submitted")
    evidence.record("task_discovered")
    evidence.record("exception", error_class="TimeoutError")

    assert evidence.diagnose() == {
        "last_completed_stage": "task_discovered",
        "next_expected_stage": "task_terminal",
    }
