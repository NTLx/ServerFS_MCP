"""Workdir-level Agent Bridge policy and host cwd confinement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import lease_identity
from .errors import BridgeError
from .models import KNOWN_RUNTIME_NAMES, AgentMode, AgentProfile

_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_REPO_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class RepoActionPolicy:
    """One operator-defined deterministic repository action.

    Command profiles accept an optional policy-valid path/basename. Built-in
    release profiles accept only a strict semver release tag for a fixed repository.
    Executables, workflow and Git arguments are never request-controlled.
    """

    name: str
    argv: tuple[str, ...] = ()
    mutates: bool = False
    argument: str = "none"
    integrity: tuple[tuple[str, str], ...] = ()

    kind: str = "command"
    repository: str | None = None
    github_cli: str = "gh"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _REPO_ACTION_RE.fullmatch(self.name) is None:
            raise ValueError("repo action name has an invalid format")
        if not isinstance(self.argv, tuple) or (self.kind == "command" and not self.argv):
            raise ValueError("repo action argv must be a non-empty tuple")
        if len(self.argv) > 32:
            raise ValueError("repo action argv must contain at most 32 items")
        for item in self.argv:
            if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 4096:
                raise ValueError("repo action argv contains an invalid item")
            if any(char in item for char in ("\x00", "\n", "\r")):
                raise ValueError("repo action argv contains an invalid character")
        if type(self.mutates) is not bool:
            raise ValueError("repo action mutates must be a boolean")
        if not isinstance(self.kind, str) or self.kind not in {
            "command",
            "release_tag",
            "publish_npm",
        }:
            raise ValueError("repo action kind must be command, release_tag or publish_npm")
        if self.argument not in {"none", "path", "basename", "release_tag"}:
            raise ValueError("repo action argument must be none, path, basename or release_tag")
        if self.kind == "command":
            if self.repository is not None or self.argument == "release_tag":
                raise ValueError("command actions cannot accept release tags or repository policy")
        elif (
            self.argv
            or self.argument != "release_tag"
            or not self.mutates
            or not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", self.repository)
            is None
        ):
            raise ValueError(
                "release actions require fixed repository, release_tag, mutates=true and no argv"
            )
        if (
            not isinstance(self.github_cli, str)
            or not self.github_cli
            or any(c in self.github_cli for c in ("\x00", "\n", "\r"))
            or (self.github_cli != "gh" and not Path(self.github_cli).is_absolute())
            or (self.kind != "publish_npm" and self.github_cli != "gh")
        ):
            raise ValueError("github_cli must be gh or a fixed absolute path for publish_npm")
        if not isinstance(self.integrity, tuple) or not self.integrity:
            raise ValueError("repo action integrity must contain at least one pinned file")
        if len(self.integrity) > 32:
            raise ValueError("repo action integrity must contain at most 32 pinned files")
        if self.kind != "command" and not {
            "package.json",
            ".github/workflows/publish.yml",
        }.issubset(
            {item[0] for item in self.integrity if isinstance(item, tuple) and len(item) == 2}
        ):
            raise ValueError(
                "release actions must pin package.json and .github/workflows/publish.yml"
            )
        seen_integrity_paths: set[str] = set()
        for item in self.integrity:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("repo action integrity entries must be path/digest pairs")
            path, digest = item
            if (
                not isinstance(path, str)
                or not path
                or path.startswith(("/", "-"))
                or "\\" in path
                or "\x00" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                raise ValueError("repo action integrity contains an invalid relative path")
            if path in seen_integrity_paths:
                raise ValueError("repo action integrity paths must be unique")
            seen_integrity_paths.add(path)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("repo action integrity digest must be lowercase sha256")


@dataclass(frozen=True)
class WorkdirAgentPolicy:
    """One workdir's Agent policy plus the lease identity it keys on (§5.3).

    ``slot`` is populated only by the legacy Compose deployment, which must keep the `NN.lock`
    artifact names a running Bridge already created. A native deployment has no slots at all and is
    keyed by the exact alias; the artifact names then follow from ``lease_identity``.
    """

    slot: int | None
    alias: str
    host_path: Path
    mode: AgentMode = AgentMode.DISABLED
    runtimes: frozenset[str] = frozenset()
    read_only: bool = True
    repo_ops_enabled: bool = False
    repo_actions: tuple[RepoActionPolicy, ...] = ()

    def __post_init__(self) -> None:
        if self.slot is not None and (
            type(self.slot) is not int or not 1 <= self.slot <= lease_identity.MAX_WORKDIR_SLOTS
        ):
            raise ValueError("slot must be between 1 and 16")
        if not isinstance(self.alias, str) or _ALIAS_RE.fullmatch(self.alias) is None:
            raise ValueError("alias has an invalid format")
        if not isinstance(self.host_path, Path):
            raise ValueError("host_path must be a Path")
        if not self.host_path.is_absolute():
            raise ValueError("host_path must be absolute")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if type(self.repo_ops_enabled) is not bool:
            raise ValueError("repo_ops_enabled must be a boolean")
        if not isinstance(self.repo_actions, tuple) or any(
            not isinstance(action, RepoActionPolicy) for action in self.repo_actions
        ):
            raise ValueError("repo_actions must contain RepoActionPolicy values")
        if not self.repo_ops_enabled and self.repo_actions:
            raise ValueError("repo actions require repo_ops_enabled")
        action_names = [action.name for action in self.repo_actions]
        if len(action_names) != len(set(action_names)):
            raise ValueError("repo action names must be unique")
        if self.read_only and self.repo_actions:
            raise ValueError("read-only workdir cannot configure executable repo actions")
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

    @property
    def lease_id(self) -> str:
        """The platform-neutral writer-lease key for this workdir (§5.3)."""
        if self.slot is None:
            return lease_identity.alias_lease_id(self.alias)
        return lease_identity.slot_lease_id(self.slot)


class PolicyRegistry:
    def __init__(self, policies: list[WorkdirAgentPolicy]):
        self._by_alias: dict[str, WorkdirAgentPolicy] = {}
        self._by_lease: dict[str, WorkdirAgentPolicy] = {}
        for policy in policies:
            if policy.alias in self._by_alias:
                raise ValueError(f"duplicate workdir alias: {policy.alias}")
            if policy.lease_id in self._by_lease:
                raise ValueError(f"duplicate workdir lease: {policy.lease_id}")
            self._by_alias[policy.alias] = policy
            self._by_lease[policy.lease_id] = policy

    def get(self, alias: str) -> WorkdirAgentPolicy:
        policy = self._by_alias.get(alias)
        if policy is None:
            raise BridgeError("WORKDIR_NOT_FOUND", f"unknown workdir: {alias}")
        return policy

    def lease_ids(self) -> tuple[str, ...]:
        """Every configured lease key, in configuration order.

        The Bridge pre-creates one artifact per key because the mutation reader may never create
        one (§5.2) — including for workdirs whose Agent mode is not workspace-write, because a
        ServerFS mutation takes the lease wherever Agent integration is enabled.
        """
        return tuple(policy.lease_id for policy in self._by_lease.values())

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
    # §8.2: relative_cwd is a virtual "/" path on every platform. A backslash or a
    # drive prefix is native host-path syntax, and on Windows `Path("C:\\dir")`
    # would otherwise be joined as an absolute path and accepted whenever it
    # happened to land inside the workdir.
    if "\\" in relative_cwd:
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd must use / separators")
    if _DRIVE_PREFIX_RE.match(relative_cwd):
        raise BridgeError("INVALID_WORKDIR_PATH", "relative cwd must not be a drive path")
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
