"""Optional Jev-backed advisory preflight for delegated Agent tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

JEV_MODEL = "jev-1.13.0"
_EXECUTION_FIT_CHOICES = ("structured_serverfs", "native_agent", "unclear")
_ROUTE_CHOICES = ("direct_serverfs_tool", "codex", "claude", "human_review")


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
                    "true": (
                        "One bounded outcome that can be completed and verified as one task. "
                        "Multiple ordered commands or steps still count as one objective when they "
                        "jointly complete or verify that same outcome."
                    ),
                    "false": (
                        "Two or more independent outcomes, or an open-ended exploratory, planning, "
                        "or prioritization request without one bounded deliverable."
                    ),
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
                        "deployment, network research, or broad coding-agent reasoning."
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
            "route_recommendation": {
                "type": "choice",
                "instructions": (
                    "Recommend the best ServerFS execution route for this task independently of "
                    "the already requested runtime. This is advisory only. Choose the route that "
                    "best matches the task's required capabilities and explicit provider intent."
                ),
                "criteria": {
                    "direct_serverfs_tool": (
                        "Use bounded ServerFS filesystem primitives directly. Choose this when "
                        "list/find/search/read/stat/create/edit/delete/upload/download are "
                        "sufficient and no shell, Git, test runner, build, deployment, "
                        "provider-native session, "
                        "or broad coding-agent reasoning is required."
                    ),
                    "codex": (
                        "Use the Codex native Agent route. Choose this for shell commands, Git, "
                        "tests, builds, deployment, general coding-agent work, or when the task "
                        "explicitly requests Codex or requires live steering, unless it explicitly "
                        "requires Claude-specific state or tooling."
                    ),
                    "claude": (
                        "Use the Claude Code native Agent route. Choose this when the task "
                        "explicitly requests Claude/Claude Code or requires Claude-specific "
                        "sessions, settings, "
                        "skills, or provider-native behavior."
                    ),
                    "human_review": (
                        "Do not choose an automated execution route yet. Choose this when the task "
                        "requires human authorization or business judgment, is too ambiguous or "
                        "underspecified to route responsibly, or asks for open-ended "
                        "prioritization "
                        "such as broadly reviewing a repository to decide what should be done next "
                        "without explicit decision criteria."
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
                "execution_fit": _choice_value(
                    answers["execution_fit"],
                    allowed_choices=_EXECUTION_FIT_CHOICES,
                    label="execution_fit",
                ),
                "route_recommendation": _choice_value(
                    answers["route_recommendation"],
                    allowed_choices=_ROUTE_CHOICES,
                    label="route_recommendation",
                ),
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


def _choice_value(
    answer: Any,
    *,
    allowed_choices: tuple[str, ...] = _EXECUTION_FIT_CHOICES,
    label: str = "execution_fit",
) -> dict[str, Any]:
    choice = getattr(answer, "choice", None)
    confidence = getattr(answer, "confidence", None)
    probabilities = getattr(answer, "probabilities", None)
    if choice not in allowed_choices:
        raise ValueError(f"Jev returned an unknown {label} choice")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Jev returned an invalid Choice confidence")
    confidence_number = float(confidence)
    if not 0.0 <= confidence_number <= 1.0:
        raise ValueError("Jev returned a Choice confidence outside 0..1")
    if not isinstance(probabilities, dict):
        raise ValueError("Jev returned invalid Choice probabilities")

    normalized_probabilities: dict[str, float] = {}
    for key in allowed_choices:
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
