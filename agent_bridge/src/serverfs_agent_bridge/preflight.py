"""Optional Jev-backed advisory preflight for delegated Agent tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

JEV_MODEL = "jev-1.13.0"


class TaskPreflight(Protocol):
    async def evaluate(
        self,
        *,
        runtime: str,
        workdir: str,
        path: str,
        profile: str,
        prompt: str,
        is_continuation: bool,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass
class JevTaskPreflight:
    """Evaluate delegation quality without changing task authorization or execution."""

    _client: Any
    model: str = JEV_MODEL

    @classmethod
    def from_api_key(cls, api_key: str) -> JevTaskPreflight:
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        client = AsyncTypeSafeClient(
            api_key=api_key,
            model=JEV_MODEL,
            timeout=5.0,
            retry=RetryPolicy(max_retries=1, timeout=8.0),
        )
        return cls(_client=client)

    async def close(self) -> None:
        await self._client.aclose()

    async def evaluate(
        self,
        *,
        runtime: str,
        workdir: str,
        path: str,
        profile: str,
        prompt: str,
        is_continuation: bool,
    ) -> dict[str, Any]:
        state = {
            "runtime": runtime,
            "workdir": workdir,
            "path": path,
            "profile": profile,
            "is_continuation": is_continuation,
            "prompt": prompt,
        }
        questions = {
            "single_objective": {
                "type": "noul",
                "instructions": (
                    "Does the delegated task describe one narrow objective rather than "
                    "multiple independent objectives bundled together?"
                ),
                "criteria": {
                    "true": "One bounded objective that can be completed and verified as one task.",
                    "false": "Two or more independent objectives or an open-ended bundle of work.",
                },
            },
            "mutation_boundary_explicit": {
                "type": "noul",
                "instructions": (
                    "Is the allowed mutation scope explicit, including an explicit read-only "
                    "or no-change boundary when no mutation is intended?"
                ),
                "criteria": {
                    "true": (
                        "The task says what may change, or clearly says that nothing may change."
                    ),
                    "false": (
                        "The mutation boundary is absent, ambiguous, or effectively unlimited."
                    ),
                },
            },
            "stop_condition_explicit": {
                "type": "noul",
                "instructions": (
                    "Does the task contain a clear stop condition, including when to stop on "
                    "failure or when the requested objective is complete?"
                ),
                "criteria": {
                    "true": "A delegated agent can tell when it must stop and return.",
                    "false": (
                        "The task can expand indefinitely or has no clear completion boundary."
                    ),
                },
            },
            "verification_evidence_explicit": {
                "type": "noul",
                "instructions": (
                    "Does the task request concrete verification evidence or an observable result "
                    "that can be checked after execution?"
                ),
                "criteria": {
                    "true": (
                        "The requested result includes verifiable evidence, checks, or exact state."
                    ),
                    "false": "Success is subjective or no verification evidence is requested.",
                },
            },
            "execution_fit": {
                "type": "choice",
                "instructions": (
                    "Which execution path best fits this task as written? Choose based on the "
                    "capabilities required to complete the whole task."
                ),
                "criteria": {
                    "structured_serverfs": (
                        "The task can be completed entirely with bounded ServerFS filesystem "
                        "primitives such as list, find, search, read, stat, create, edit, delete, "
                        "upload, or download, without shell commands, builds/tests, Git, "
                        "deployment, "
                        "network research, or broad coding-agent reasoning."
                    ),
                    "native_agent": (
                        "The task requires coding-agent capabilities such as shell commands, "
                        "builds/tests, Git, deployment, provider-native tools, or substantial "
                        "open-ended code reasoning."
                    ),
                    "unclear": (
                        "The task is underspecified, mixes incompatible execution needs, or lacks "
                        "enough information to choose confidently."
                    ),
                },
            },
        }

        response = await self._client.system_one(
            state=state,
            questions=questions,
            model=self.model,
        )

        answers = response.answers
        normalized = {
            "status": "completed",
            "model": response.model,
            "answers": {
                "single_objective": _noul_value(answers["single_objective"]),
                "mutation_boundary_explicit": _noul_value(answers["mutation_boundary_explicit"]),
                "stop_condition_explicit": _noul_value(answers["stop_condition_explicit"]),
                "verification_evidence_explicit": _noul_value(
                    answers["verification_evidence_explicit"]
                ),
                "execution_fit": _choice_value(answers["execution_fit"]),
            },
        }
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        if type(input_tokens) is int and input_tokens >= 0:
            normalized["usage"] = {"input_tokens": input_tokens}
        return normalized


def _noul_value(answer: Any) -> float:
    value = getattr(answer, "noul", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Jev returned an invalid Noul answer")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("Jev returned a Noul answer outside 0..1")
    return number


def _choice_value(answer: Any) -> dict[str, Any]:
    choice = getattr(answer, "choice", None)
    confidence = getattr(answer, "confidence", None)
    probabilities = getattr(answer, "probabilities", None)
    if choice not in {"structured_serverfs", "native_agent", "unclear"}:
        raise ValueError("Jev returned an unknown execution_fit choice")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Jev returned an invalid Choice confidence")
    confidence_number = float(confidence)
    if not 0.0 <= confidence_number <= 1.0:
        raise ValueError("Jev returned a Choice confidence outside 0..1")
    if not isinstance(probabilities, dict):
        raise ValueError("Jev returned invalid Choice probabilities")

    normalized_probabilities: dict[str, float] = {}
    for key in ("structured_serverfs", "native_agent", "unclear"):
        value = probabilities.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Jev returned invalid Choice probabilities")
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError("Jev returned a Choice probability outside 0..1")
        normalized_probabilities[key] = number

    return {
        "choice": choice,
        "confidence": confidence_number,
        "probabilities": normalized_probabilities,
    }
