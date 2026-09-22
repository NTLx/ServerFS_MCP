from __future__ import annotations

from types import SimpleNamespace

import pytest

from serverfs_agent_bridge.preflight import JevTaskPreflight, _choice_value, _noul_value


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="jev-1.13.0",
            answers={
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
            },
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
    }

    await preflight.close()
    assert client.closed is True


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
