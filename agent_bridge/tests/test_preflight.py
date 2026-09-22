from __future__ import annotations

from types import SimpleNamespace

import pytest

from serverfs_agent_bridge.preflight import (
    JevTaskPreflight,
    _approval_state,
    _choice_value,
    _noul_value,
    _sanitize_advisor_value,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def system_one(self, **kwargs):
        self.calls.append(kwargs)
        questions = kwargs["questions"]
        if "necessary_for_objective" in questions:
            answers = {
                "necessary_for_objective": SimpleNamespace(noul=0.92),
                "scope_bounded": SimpleNamespace(noul=0.88),
                "destructive_or_irreversible": SimpleNamespace(noul=0.12),
                "sensitive_access": SimpleNamespace(noul=0.08),
                "external_side_effect": SimpleNamespace(noul=0.05),
                "recommendation": SimpleNamespace(
                    choice="approve_once",
                    confidence=0.9,
                    probabilities={
                        "approve_once": 0.9,
                        "approve_session": 0.04,
                        "deny": 0.02,
                        "cancel_task": 0.01,
                        "review_carefully": 0.03,
                    },
                ),
            }
        else:
            answers = {
                "single_objective": SimpleNamespace(noul=0.95),
                "mutation_boundary_explicit": SimpleNamespace(noul=0.9),
                "stop_condition_explicit": SimpleNamespace(noul=0.8),
                "verification_evidence_explicit": SimpleNamespace(noul=0.7),
                "execution_fit": SimpleNamespace(
                    choice="native_agent",
                    confidence=0.85,
                    probabilities={
                        "structured_serverfs": 0.1,
                        "native_agent": 0.85,
                        "unclear": 0.05,
                    },
                ),
                "route_recommendation": SimpleNamespace(
                    choice="codex",
                    confidence=0.8,
                    probabilities={
                        "direct_serverfs_tool": 0.05,
                        "codex": 0.8,
                        "claude": 0.1,
                        "human_review": 0.05,
                    },
                ),
            }
        return SimpleNamespace(
            model="jev-1.13.0",
            answers=answers,
            usage=SimpleNamespace(input_tokens=321),
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_jev_preflight_returns_normalized_advisory_result() -> None:
    client = FakeClient()
    preflight = JevTaskPreflight(client)

    result = await preflight.evaluate(
        runtime="codex",
        workdir="ServerFS",
        path="agent_bridge",
        profile="workspace-write",
        prompt="Implement one change, run its tests, and stop if they fail.",
        is_continuation=False,
    )

    assert result == {
        "status": "completed",
        "model": "jev-1.13.0",
        "answers": {
            "single_objective": 0.95,
            "mutation_boundary_explicit": 0.9,
            "stop_condition_explicit": 0.8,
            "verification_evidence_explicit": 0.7,
            "execution_fit": {
                "choice": "native_agent",
                "confidence": 0.85,
                "probabilities": {
                    "structured_serverfs": 0.1,
                    "native_agent": 0.85,
                    "unclear": 0.05,
                },
            },
            "route_recommendation": {
                "choice": "codex",
                "confidence": 0.8,
                "probabilities": {
                    "direct_serverfs_tool": 0.05,
                    "codex": 0.8,
                    "claude": 0.1,
                    "human_review": 0.05,
                },
            },
        },
        "usage": {"input_tokens": 321},
    }
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "jev-1.13.0"
    assert call["state"] == {
        "runtime": "codex",
        "workdir": "ServerFS",
        "path": "agent_bridge",
        "profile": "workspace-write",
        "is_continuation": False,
        "prompt": "Implement one change, run its tests, and stop if they fail.",
    }
    questions = call["questions"]
    assert set(questions) == {
        "single_objective",
        "mutation_boundary_explicit",
        "stop_condition_explicit",
        "verification_evidence_explicit",
        "execution_fit",
        "route_recommendation",
    }

    await preflight.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_jev_approval_advisor_returns_normalized_advice() -> None:
    client = FakeClient()
    advisor = JevTaskPreflight(client)

    result = await advisor.advise_approval(
        runtime="codex",
        workdir="ServerFS",
        path="agent_bridge",
        profile="workspace-write",
        prompt="Run one bounded test command and report the result.",
        approval={
            "category": "command",
            "title": "Codex command approval",
            "command_display": "uv run pytest agent_bridge/tests/test_preflight.py -q",
            "available_decisions": ["approve_once", "approve_session", "deny", "cancel_task"],
        },
    )

    assert result["status"] == "completed"
    assert result["automatic"] is False
    assert result["recommended_decision_available"] is True
    answers = result["answers"]
    assert answers["necessary_for_objective"] == 0.92
    assert answers["scope_bounded"] == 0.88
    assert answers["destructive_or_irreversible"] == 0.12
    assert answers["sensitive_access"] == 0.08
    assert answers["external_side_effect"] == 0.05
    assert answers["recommendation"]["choice"] == "approve_once"

    call = client.calls[0]
    assert call["state"]["task"]["prompt"] == (
        "Run one bounded test command and report the result."
    )
    assert call["state"]["approval"]["category"] == "command"
    assert set(call["questions"]) == {
        "necessary_for_objective",
        "scope_bounded",
        "destructive_or_irreversible",
        "sensitive_access",
        "external_side_effect",
        "recommendation",
    }


def test_approval_prompt_sanitizer_redacts_and_truncates() -> None:
    prompt = "Run check with token=abc123 and Authorization: Bearer secret-token " + "x" * 5000
    sanitized = _sanitize_advisor_value(prompt)
    assert "abc123" not in sanitized
    assert "secret-token" not in sanitized
    assert "<redacted>" in sanitized
    assert len(sanitized) <= 4097


def test_approval_state_whitelists_and_redacts_sensitive_values() -> None:
    state = _approval_state(
        {
            "category": "command",
            "command_display": "curl -H 'Authorization: Bearer abc123' --token xyz",
            "available_decisions": ["approve_once", "deny"],
            "tool_input": {"file_path": "ignored", "content": "ignored"},
            "additional_permissions": {
                "api_key": "secret-value",
                "nested": {"password": "hunter2", "mode": "network"},
            },
        }
    )

    assert "tool_input" not in state
    assert "abc123" not in state["command_display"]
    assert "xyz" not in state["command_display"]
    assert state["additional_permissions"]["api_key"] == "<redacted>"
    assert state["additional_permissions"]["nested"]["password"] == "<redacted>"
    assert state["additional_permissions"]["nested"]["mode"] == "network"


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "0.5", None])
def test_noul_value_rejects_invalid_answers(value) -> None:
    with pytest.raises(ValueError, match="Jev returned"):
        _noul_value(SimpleNamespace(noul=value))


def test_choice_value_rejects_unknown_choice() -> None:
    with pytest.raises(ValueError, match="unknown execution_fit"):
        _choice_value(
            SimpleNamespace(
                choice="other",
                confidence=0.8,
                probabilities={
                    "structured_serverfs": 0.1,
                    "native_agent": 0.8,
                    "unclear": 0.1,
                },
            )
        )


def test_choice_value_supports_runtime_route_choices() -> None:
    result = _choice_value(
        SimpleNamespace(
            choice="claude",
            confidence=0.7,
            probabilities={
                "direct_serverfs_tool": 0.05,
                "codex": 0.15,
                "claude": 0.7,
                "human_review": 0.1,
            },
        ),
        allowed_choices=("direct_serverfs_tool", "codex", "claude", "human_review"),
        label="route_recommendation",
    )
    assert result["choice"] == "claude"
    assert result["probabilities"]["human_review"] == 0.1
