"""Optional Jev-backed advisory preflight for delegated Agent tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

JEV_MODEL = "jev-1.13.0"
_EXECUTION_FIT_CHOICES = ("structured_serverfs", "native_agent", "unclear")
_ROUTE_CHOICES = ("direct_serverfs_tool", "codex", "claude", "human_review")
_APPROVAL_RECOMMENDATIONS = (
    "approve_once",
    "approve_session",
    "deny",
    "cancel_task",
    "review_carefully",
)
_APPROVAL_STATE_KEYS = frozenset(
    {
        "category",
        "title",
        "reason",
        "command_display",
        "relative_cwd",
        "additional_permissions",
        "network_approval_context",
        "file_changes",
        "requested_permissions",
        "tool",
        "blocked_path",
        "available_decisions",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
)
_MAX_ADVISOR_TEXT_CHARS = 4096
_MAX_ADVISOR_LIST_ITEMS = 50
_AUTHORIZATION_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*)([^\r\n'\";]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_FLAG_RE = re.compile(r"(?i)(--?(?:api[-_]?key|token|password|passwd|secret)\s+)(\S+)")
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")


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

    async def advise_approval(
        self,
        *,
        runtime: str,
        workdir: str,
        path: str,
        profile: str,
        prompt: str,
        approval: dict[str, Any],
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

    async def advise_approval(
        self,
        *,
        runtime: str,
        workdir: str,
        path: str,
        profile: str,
        prompt: str,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        state = {
            "task": {
                "runtime": runtime,
                "workdir": workdir,
                "path": path,
                "profile": profile,
                "prompt": _sanitize_advisor_value(prompt),
            },
            "approval": _approval_state(approval),
        }
        questions = {
            "necessary_for_objective": {
                "type": "noul",
                "instructions": (
                    "Is this approval request materially necessary to complete the authorized "
                    "task objective as written?"
                ),
                "criteria": {
                    "true": (
                        "The requested capability or action is directly needed to complete the "
                        "stated task."
                    ),
                    "false": (
                        "The request is unrelated, avoidable, or broader than what the stated "
                        "task requires."
                    ),
                },
            },
            "scope_bounded": {
                "type": "noul",
                "instructions": (
                    "Is the requested permission or action narrowly scoped and proportionate to "
                    "the task objective?"
                ),
                "criteria": {
                    "true": (
                        "The request grants only the minimum practical scope, target, duration, "
                        "and side effects needed for the task."
                    ),
                    "false": (
                        "The request is broad, session-wide without clear need, crosses unrelated "
                        "resources, or grants materially more authority than necessary."
                    ),
                },
            },
            "destructive_or_irreversible": {
                "type": "noul",
                "instructions": (
                    "Could approving this request cause destructive, irreversible, or difficult "
                    "to recover changes?"
                ),
                "criteria": {
                    "true": (
                        "It may delete, overwrite, reset, publish, deploy, permanently alter, or "
                        "otherwise create hard-to-reverse effects."
                    ),
                    "false": (
                        "The action is read-only or readily reversible with a small, contained "
                        "blast radius."
                    ),
                },
            },
            "sensitive_access": {
                "type": "noul",
                "instructions": (
                    "Does this request involve sensitive access such as credentials, secrets, "
                    "protected paths, elevated permissions, or unusually broad data access?"
                ),
                "criteria": {
                    "true": (
                        "The request reaches secrets, credentials, protected/outside resources, "
                        "or elevated/broad capabilities."
                    ),
                    "false": (
                        "The request stays within ordinary task-scoped resources and capabilities."
                    ),
                },
            },
            "external_side_effect": {
                "type": "noul",
                "instructions": (
                    "Could approving this request create a side effect outside the local workdir "
                    "or local analysis state?"
                ),
                "criteria": {
                    "true": (
                        "It may push, publish, deploy, send, write to a remote service, mutate an "
                        "external system, or otherwise affect something outside the local workdir."
                    ),
                    "false": (
                        "It is confined to local inspection or local workdir changes with no "
                        "external mutation."
                    ),
                },
            },
            "recommendation": {
                "type": "choice",
                "instructions": (
                    "Recommend how a human reviewer should handle this approval request. The "
                    "recommendation is advisory only. Respect the provider's available decisions "
                    "listed in the state; use review_carefully when context or risk is too "
                    "uncertain for a concrete decision."
                ),
                "criteria": {
                    "approve_once": (
                        "The request appears necessary, narrow, and acceptable for one operation "
                        "or one turn. Prefer this over broader approval when one-time scope is "
                        "sufficient."
                    ),
                    "approve_session": (
                        "The same narrow capability is clearly needed repeatedly for this task, "
                        "session scope is explicitly offered, and the capability is not broadly "
                        "sensitive, destructive, or externally consequential."
                    ),
                    "deny": (
                        "The request is unnecessary, overly broad, materially misaligned with the "
                        "task, or presents risk that is not justified by the objective."
                    ),
                    "cancel_task": (
                        "The request indicates the task has materially departed from its "
                        "authorized objective or continuing the task itself appears inappropriate."
                    ),
                    "review_carefully": (
                        "A human should inspect the request details before deciding because "
                        "impact, necessity, scope, or available context is materially uncertain."
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
        recommendation = _choice_value(
            answers["recommendation"],
            allowed_choices=_APPROVAL_RECOMMENDATIONS,
            label="approval recommendation",
        )
        available = approval.get("available_decisions")
        available_decisions = (
            {item for item in available if isinstance(item, str)}
            if isinstance(available, list)
            else set()
        )
        recommended = recommendation["choice"]
        decision_available = recommended == "review_carefully" or (
            recommended in available_decisions
        )

        normalized = {
            "status": "completed",
            "model": response.model,
            "answers": {
                "necessary_for_objective": _noul_value(answers["necessary_for_objective"]),
                "scope_bounded": _noul_value(answers["scope_bounded"]),
                "destructive_or_irreversible": _noul_value(answers["destructive_or_irreversible"]),
                "sensitive_access": _noul_value(answers["sensitive_access"]),
                "external_side_effect": _noul_value(answers["external_side_effect"]),
                "recommendation": recommendation,
            },
            "recommended_decision_available": decision_available,
            "automatic": False,
        }
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        if type(input_tokens) is int and input_tokens >= 0:
            normalized["usage"] = {"input_tokens": input_tokens}
        return normalized

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


def _approval_state(approval: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _sanitize_advisor_value(value)
        for key, value in approval.items()
        if key in _APPROVAL_STATE_KEYS
    }


def _sanitize_advisor_value(value: Any) -> Any:
    if isinstance(value, str):
        text = _AUTHORIZATION_HEADER_RE.sub(r"\1<redacted>", value)
        text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
        text = _SECRET_FLAG_RE.sub(r"\1<redacted>", text)
        text = _BEARER_RE.sub(r"\1 <redacted>", text)
        if len(text) > _MAX_ADVISOR_TEXT_CHARS:
            return text[:_MAX_ADVISOR_TEXT_CHARS] + "…"
        return text
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = _sanitize_advisor_value(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        items = list(value[:_MAX_ADVISOR_LIST_ITEMS])
        return [_sanitize_advisor_value(item) for item in items]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_ADVISOR_TEXT_CHARS]


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
