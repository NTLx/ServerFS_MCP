"""Workdir-level Agent Bridge policy and host cwd confinement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import BridgeError
from .models import KNOWN_RUNTIME_NAMES, AgentMode, AgentProfile

_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_MAX_WORKDIR_SLOTS = 16


@dataclass(frozen=True)
class WorkdirAgentPolicy:
    slot: int
    alias: str
    host_path: Path
    mode: AgentMode = AgentMode.DISABLED
    runtimes: frozenset[str] = frozenset()
    read_only: bool = True

    def __post_init__(self) -> None:
        if type(self.slot) is not int or not 1 <= self.slot <= _MAX_WORKDIR_SLOTS:
            raise ValueError("slot must be between 1 and 16")
        if not isinstance(self.alias, str) or _ALIAS_RE.fullmatch(self.alias) is None:
            raise ValueError("alias has an invalid format")
        if not isinstance(self.host_path, Path):
            raise ValueError("host_path must be a Path")
        if not self.host_path.is_absolute():
            raise ValueError("host_path must be absolute")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if not isinstance(self.mode, AgentMode):
            raise ValueError("mode must be a valid AgentMode")
        if not isinstance(self.runtimes, frozenset) or any(
            not isinstance(runtime, str) or runtime not in KNOWN_RUNTIME_NAMES
            for runtime in self.runtimes
        ):
            raise ValueError("runtimes contains an unknown runtime")
        try:
            resolved = self.host_path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("host_path must exist") from exc
        if not resolved.is_dir():
            raise ValueError("host_path must be a directory")
        object.__setattr__(self, "host_path", resolved)
        if self.mode is AgentMode.WORKSPACE_WRITE and self.read_only:
            raise ValueError("workspace-write agent mode requires a writable workdir")


class PolicyRegistry:
    def __init__(self, policies: list[WorkdirAgentPolicy]):
        self._by_alias: dict[str, WorkdirAgentPolicy] = {}
        self._by_slot: dict[int, WorkdirAgentPolicy] = {}
        for policy in policies:
            if policy.alias in self._by_alias:
                raise ValueError(f"duplicate workdir alias: {policy.alias}")
            if policy.slot in self._by_slot:
                raise ValueError(f"duplicate workdir slot: {policy.slot}")
            self._by_alias[policy.alias] = policy
            self._by_slot[policy.slot] = policy

    def get(self, alias: str) -> WorkdirAgentPolicy:
        policy = self._by_alias.get(alias)
        if policy is None:
            raise BridgeError("WORKDIR_NOT_FOUND", f"unknown workdir: {alias}")
        return policy

    def authorize(
        self,
        *,
        workdir: str,
        runtime: str,
        profile: AgentProfile | str,
        relative_cwd: str,
    ) -> tuple[WorkdirAgentPolicy, Path]:
        policy = self.get(workdir)
        try:
            requested = AgentProfile(profile)
        except (TypeError, ValueError) as exc:
            raise BridgeError("AGENT_PROFILE_NOT_ALLOWED", "unknown agent profile") from exc

        if policy.mode is AgentMode.DISABLED:
            raise BridgeError("AGENT_DISABLED", f"agent execution is disabled for {workdir}")
        if runtime not in policy.runtimes:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_ALLOWED",
                f"runtime {runtime} is not enabled for {workdir}",
            )
        if (
            requested is AgentProfile.WORKSPACE_WRITE
            and policy.mode is not AgentMode.WORKSPACE_WRITE
        ):
            raise BridgeError(
                "AGENT_PROFILE_NOT_ALLOWED",
                f"profile {requested.value} exceeds the configured mode for {workdir}",
            )
        if requested is AgentProfile.WORKSPACE_WRITE and policy.read_only:
            raise BridgeError(
                "AGENT_PROFILE_NOT_ALLOWED",
                f"workspace-write is not allowed on read-only workdir {workdir}",
            )
        return policy, resolve_relative_cwd(policy.host_path, relative_cwd)


def redact_host_path(root: Path, candidate: str | Path) -> str:
    """Return a workdir-relative display path without leaking the host root.

    Paths outside the configured workdir are deliberately collapsed to a
    constant marker rather than partially revealing the host layout.
    """
    try:
        resolved_root = root.resolve(strict=True)
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate_path = resolved_root / candidate_path
        resolved_candidate = candidate_path.resolve(strict=False)
        relative = resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return "<outside-workdir>"
    return "." if not relative.parts else relative.as_posix()


def resolve_relative_cwd(root: Path, relative_cwd: str) -> Path:
    if not isinstance(relative_cwd, str):
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd must be a string")
    if "\x00" in relative_cwd:
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd contains NUL")
    pure = PurePosixPath(relative_cwd or ".")
    if pure.is_absolute():
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd must not be absolute")
    if any(part == ".." for part in pure.parts):
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd must not contain '..'")

    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / Path(*[p for p in pure.parts if p not in ("", ".")])).resolve(
            strict=True
        )
    except (FileNotFoundError, OSError) as exc:
        raise BridgeError("WORKDIR_PATH_NOT_FOUND", "agent cwd does not exist") from exc

    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BridgeError(
            "INVALID_WORKDIR_PATH", "agent cwd escapes the configured workdir"
        ) from exc
    if not candidate.is_dir():
        raise BridgeError("INVALID_WORKDIR_PATH", "agent cwd must be a directory")
    return candidate
