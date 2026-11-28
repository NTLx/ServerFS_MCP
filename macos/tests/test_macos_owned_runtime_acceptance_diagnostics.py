from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/macos/v0250_owned_core_direct_runtime_acceptance.py"
)
_SPEC = importlib.util.spec_from_file_location("owned_runtime_acceptance", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
task_diagnostic = _MODULE.task_diagnostic


def test_owned_runtime_task_diagnostic_redacts_result_and_event_payloads() -> None:
    private_text = "provider-private-response-and-error"
    result = task_diagnostic(
        {
            "status": "failed",
            "error_code": "AGENT_PROVIDER_ERROR",
            "error_message": private_text,
            "final_response": private_text,
        },
        "expected-marker",
        [
            {"event_type": "task.failed", "payload": {"message": private_text}},
            {"event_type": "unknown.private.event", "payload": {"text": private_text}},
        ],
    )

    rendered = json.dumps(result, sort_keys=True)
    assert private_text not in rendered
    assert set(result) == {
        "status",
        "error_code",
        "provider_completion_subtype",
        "result_bytes",
        "result_sha256",
        "marker_match",
        "event_types",
    }
    assert result["status"] == "failed"
    assert result["error_code"] == "AGENT_PROVIDER_ERROR"
    assert result["provider_completion_subtype"] is None
    assert result["result_bytes"] == len(private_text.encode())
    assert result["marker_match"] == "absent"
    assert result["event_types"] == ["task.failed"]


def test_owned_runtime_task_diagnostic_distinguishes_marker_categories() -> None:
    assert (
        task_diagnostic({"status": "succeeded", "final_response": "  MARK  "}, "MARK")[
            "marker_match"
        ]
        == "exact"
    )
    assert (
        task_diagnostic({"status": "succeeded", "final_response": "prefix MARK suffix"}, "MARK")[
            "marker_match"
        ]
        == "contains"
    )


def test_owned_runtime_task_diagnostic_summarizes_error_text_without_emitting_it() -> None:
    private_text = "private Claude provider error"
    result = task_diagnostic(
        {"status": "failed", "error_code": "AGENT_PROVIDER_ERROR", "error_message": private_text},
        "expected-marker",
    )

    assert private_text not in json.dumps(result)
    assert result["result_bytes"] == len(private_text.encode())
    assert result["result_sha256"] == hashlib.sha256(private_text.encode()).hexdigest()
    assert result["marker_match"] == "absent"


def test_owned_runtime_task_diagnostic_does_not_emit_unknown_provider_metadata() -> None:
    secret = "arbitrary-private-provider-metadata"
    result = task_diagnostic(
        {"status": "failed", "error_code": secret, "provider_completion_subtype": secret},
        "marker",
    )
    assert result["error_code"] == "OTHER"
    assert result["provider_completion_subtype"] is None
    assert secret not in json.dumps(result)
