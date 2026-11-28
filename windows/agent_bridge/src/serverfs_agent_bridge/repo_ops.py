"""Deterministic, non-Agent repository operations executed on the host.

This module deliberately exposes no generic command surface. Git operations are fixed,
and named actions are operator-configured fixed argv profiles selected only by name.
No provider adapter, model, Agent task, shell, request-controlled executable, environment,
remote, ref, or force flag is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import BridgeError
from .policy import RepoActionPolicy, WorkdirAgentPolicy

_MAX_OUTPUT_BYTES = 256 * 1024
_READ_TIMEOUT_SECONDS = 30.0
_ACTION_TIMEOUT_SECONDS = 300.0
_PUSH_TIMEOUT_SECONDS = 120.0
_MAX_PATHS = 64
_MAX_COMMIT_MESSAGE_BYTES = 1024
_MAX_INTEGRITY_FILE_BYTES = 8 * 1024 * 1024
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
RELEASE_TAG_PATTERN = (
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.(?:0|[1-9][0-9]*))?"
)

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BASE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)
_GIT_ENV_KEYS = (*_BASE_ENV_KEYS, "SSH_AUTH_SOCK")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class _OutputLimitExceeded(Exception):
    pass


class RepoCleanupUnproven(BridgeError):
    """The writer lease must remain held until the Bridge is stopped externally."""

    def __init__(self) -> None:
        super().__init__("REPO_STOP_UNPROVEN", "repository command tree stop could not be proven")


def normalize_repo_path(value: str) -> str:
    """Validate a platform-neutral workdir-relative repository path."""
    if not isinstance(value, str) or not value:
        raise BridgeError("INVALID_REPO_PATH", "repo path must be a non-empty string")
    if "\x00" in value or "\\" in value or _DRIVE_PREFIX_RE.match(value):
        raise BridgeError("INVALID_REPO_PATH", "repo path has an invalid format")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise BridgeError("INVALID_REPO_PATH", "repo path must stay inside the workdir")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise BridgeError("INVALID_REPO_PATH", "repo path must name an entry")
    normalized = "/".join(parts)
    if normalized.startswith("-"):
        raise BridgeError("INVALID_REPO_PATH", "repo path must not begin with '-'")
    return normalized


def _literal_pathspec(value: str) -> str:
    """Turn one validated path into a Git literal pathspec."""
    return f":(literal){value}"


def _nul_paths(value: str) -> list[str]:
    return [item for item in value.split("\x00") if item]


def _validate_host_path(
    root: Path,
    value: str,
    *,
    require_existing_file: bool,
) -> None:
    """Reject links, Windows reparse points, and unsafe final objects on the host."""
    if os.name == "nt":
        from .windows_security import is_reparse_point

        current = root.absolute()
        if is_reparse_point(current):
            raise BridgeError("INVALID_REPO_PATH", "repo root must not be a reparse point")
    else:
        current = root.resolve(strict=True)
    parts = PurePosixPath(value).parts
    for index, part in enumerate(parts):
        candidate = current / part
        final = index == len(parts) - 1
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            if final and not require_existing_file:
                return
            raise BridgeError("INVALID_REPO_PATH", "repo path does not exist") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BridgeError("INVALID_REPO_PATH", "repo paths must not traverse symlinks")
        if os.name == "nt" and is_reparse_point(candidate):
            raise BridgeError("INVALID_REPO_PATH", "repo paths must not traverse reparse points")
        if not final:
            if not stat.S_ISDIR(info.st_mode):
                raise BridgeError("INVALID_REPO_PATH", "repo path parent is not a directory")
            current = candidate
            continue
        if require_existing_file and not stat.S_ISREG(info.st_mode):
            raise BridgeError("INVALID_REPO_PATH", "repo action path must be a regular file")
        if not require_existing_file and stat.S_ISDIR(info.st_mode):
            raise BridgeError("INVALID_REPO_PATH", "repo commit paths must name files")


def _command_env(*, git: bool) -> dict[str, str]:
    """Build a narrow child environment instead of forwarding Bridge secrets wholesale."""
    keys = _GIT_ENV_KEYS if git else _BASE_ENV_KEYS
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.setdefault("PATH", os.defpath)
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        # Windows side-by-side assembly loading requires SystemRoot in a custom environment.
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if git:
        if os.name == "nt" and "USERPROFILE" in os.environ:
            # Git for Windows derives HOME from USERPROFILE when HOME is absent.
            env["USERPROFILE"] = os.environ["USERPROFILE"]
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        # Environment-level protocol allowlisting is stronger than repository/user
        # protocol.<name>.allow configuration and prevents custom remote helpers.
        env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
        env["GIT_PROTOCOL_FROM_USER"] = "0"
    return env


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Stop the whole POSIX subprocess group before a writer lease can be released."""
    try:
        if os.name == "posix":
            # The group can outlive its leader. Do not skip killpg merely because the
            # immediate child has already exited: descendants may still hold stdio open.
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(65_536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _OutputLimitExceeded


async def run_bounded_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int = _MAX_OUTPUT_BYTES,
    git_environment: bool = False,
    strict_utf8: bool = False,
) -> CommandResult:
    """Run one fixed argv without a shell and with bounded time/output."""
    if not argv:
        raise BridgeError("REPO_ACTION_INVALID", "repo action argv is empty")
    if os.name == "nt":
        from .repo_ops_windows import run_windows_command

        return await run_windows_command(
            argv,
            cwd=cwd,
            env=_command_env(git=git_environment),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            strict_utf8=strict_utf8,
        )
    spawn_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        spawn_kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=_command_env(git=git_environment),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
    except FileNotFoundError as exc:
        raise BridgeError(
            "REPO_TOOL_UNAVAILABLE", "required repository tool is unavailable"
        ) from exc
    except OSError as exc:
        raise BridgeError("REPO_EXEC_FAILED", "repository operation could not start") from exc

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output_bytes))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output_bytes))
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    completion = asyncio.gather(*tasks)

    async def settle_after_kill() -> None:
        # Timeout must not cancel the pipe readers before the process group dies.
        # Let them observe EOF/connection_lost so asyncio transports are closed
        # before asyncio.run() tears down the event loop.
        await _kill_process_tree(process)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(completion, return_exceptions=True)

    try:
        async with asyncio.timeout(timeout_seconds):
            try:
                # Shield the aggregate from timeout cancellation. Otherwise the
                # stdout/stderr readers are cancelled first and their transports can
                # survive until after the event loop closes.
                stdout, stderr, returncode = await asyncio.shield(completion)
            except _OutputLimitExceeded as exc:
                await settle_after_kill()
                raise BridgeError(
                    "REPO_OUTPUT_TOO_LARGE", "repository operation output exceeded its limit"
                ) from exc
    except TimeoutError as exc:
        await settle_after_kill()
        raise BridgeError("REPO_TIMEOUT", "repository operation timed out") from exc
    except asyncio.CancelledError:
        await settle_after_kill()
        raise
    finally:
        if any(not task.done() for task in tasks):
            await settle_after_kill()

    try:
        stdout_text = stdout.decode("utf-8", errors="strict" if strict_utf8 else "replace")
        stderr_text = stderr.decode("utf-8", errors="strict" if strict_utf8 else "replace")
    except UnicodeDecodeError as exc:
        raise BridgeError(
            "REPO_OUTPUT_ENCODING_INVALID",
            "repository operation emitted non-UTF-8 output",
        ) from exc
    return CommandResult(returncode=returncode, stdout=stdout_text, stderr=stderr_text)


class RepoOpsRunner:
    """Fixed repository operations for one host Bridge service."""

    async def inspect(
        self,
        policy: WorkdirAgentPolicy,
        *,
        view: str,
        paths: list[str],
    ) -> dict[str, Any]:
        self._require_enabled(policy)
        normalized = self._normalize_paths(paths)
        if view == "actions":
            return {
                "view": "actions",
                "actions": [
                    {
                        "name": action.name,
                        "mutates": action.mutates,
                        "argument": action.argument,
                        **(
                            {
                                "kind": action.kind,
                                "repository": action.repository,
                                "tag_pattern": RELEASE_TAG_PATTERN,
                                "head_only": True,
                                "force": False,
                                "workflow": "publish.yml" if action.kind == "publish_npm" else None,
                                "scope": "clean HEAD on main; matching package version; "
                                "exact tag only; conflicts fail closed",
                            }
                            if action.kind != "command"
                            else {}
                        ),
                    }
                    for action in policy.repo_actions
                ],
            }
        await self._require_repo_root(policy)
        if view == "status":
            if normalized:
                raise BridgeError("INVALID_REQUEST", "status view does not accept paths")
            head = await self._git(
                policy,
                ("rev-parse", "HEAD"),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            if head.returncode != 0:
                raise BridgeError("REPO_STATUS_FAILED", "could not resolve repository HEAD")
            branch = await self._git(
                policy,
                ("symbolic-ref", "--quiet", "--short", "HEAD"),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            upstream = await self._git(
                policy,
                ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            unstaged = await self._git(
                policy,
                ("diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--"),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            staged = await self._git(
                policy,
                (
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            untracked = await self._git(
                policy,
                ("ls-files", "--others", "--exclude-standard", "-z", "--"),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
            if any(result.returncode != 0 for result in (unstaged, staged, untracked)):
                raise BridgeError("REPO_STATUS_FAILED", "could not inspect repository changes")
            return {
                "view": "status",
                "head": head.stdout.strip(),
                "branch": branch.stdout.strip() if branch.returncode == 0 else None,
                "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
                "unstaged_paths": _nul_paths(unstaged.stdout),
                "staged_paths": _nul_paths(staged.stdout),
                "untracked_paths": _nul_paths(untracked.stdout),
            }
        if view not in {"diff", "staged_diff"}:
            raise BridgeError("INVALID_REQUEST", "unknown repository inspection view")
        if not normalized:
            raise BridgeError("INVALID_REQUEST", f"{view} requires at least one explicit path")
        for path in normalized:
            _validate_host_path(
                policy.host_path,
                path,
                require_existing_file=False,
            )
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if view == "staged_diff":
            args.append("--cached")
        args.append("--")
        args.extend(_literal_pathspec(path) for path in normalized)
        diff = await self._git(policy, tuple(args), timeout_seconds=_READ_TIMEOUT_SECONDS)
        if diff.returncode != 0:
            raise BridgeError("REPO_DIFF_FAILED", "git diff failed")
        return {"view": view, "diff": diff.stdout}

    async def run_action(
        self,
        policy: WorkdirAgentPolicy,
        *,
        action_name: str,
        path: str | None,
        release_tag: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled(policy)
        action = self._action(policy, action_name)
        if action.kind != "command":
            if path is not None:
                raise BridgeError("INVALID_REQUEST", "release action accepts no path")
            if (
                not isinstance(release_tag, str)
                or len(release_tag) > 128
                or re.fullmatch(RELEASE_TAG_PATTERN, release_tag) is None
            ):
                raise BridgeError("INVALID_RELEASE_TAG", "expected a semver release tag only")
            await self._require_repo_root(policy)
            self._verify_action_integrity(policy, action)
            from .repo_release import run_release_action

            return await run_release_action(self, policy, action, release_tag)
        if release_tag is not None:
            raise BridgeError("INVALID_REQUEST", "command action accepts no release tag")
        normalized_path: str | None = None
        action_argument: str | None = None
        if action.argument in {"path", "basename"}:
            if path is None:
                raise BridgeError("INVALID_REQUEST", "repo action requires a path argument")
            normalized_path = normalize_repo_path(path)
            _validate_host_path(
                policy.host_path,
                normalized_path,
                require_existing_file=True,
            )
            action_argument = (
                PurePosixPath(normalized_path).name
                if action.argument == "basename"
                else normalized_path
            )
        elif path is not None:
            raise BridgeError("INVALID_REQUEST", "repo action does not accept a path argument")
        await self._require_repo_root(policy)
        self._verify_action_integrity(policy, action)
        argv = action.argv + ((action_argument,) if action_argument is not None else ())
        result = await run_bounded_command(
            argv,
            cwd=policy.host_path,
            timeout_seconds=_ACTION_TIMEOUT_SECONDS,
        )
        return {
            "action": action.name,
            "mutates": action.mutates,
            **result.to_dict(),
        }

    async def commit(
        self,
        policy: WorkdirAgentPolicy,
        *,
        paths: list[str],
        message: str,
    ) -> dict[str, Any]:
        self._require_enabled(policy)
        if policy.read_only:
            raise BridgeError("WORKDIR_READ_ONLY", "repository commit requires a writable workdir")
        normalized = self._normalize_paths(paths, require_nonempty=True)
        self._validate_commit_message(message)
        await self._require_repo_root(policy)
        for path in normalized:
            _validate_host_path(
                policy.host_path,
                path,
                require_existing_file=False,
            )

        staged_before = await self._staged_paths(policy)
        if staged_before:
            raise BridgeError(
                "REPO_INDEX_DIRTY",
                "repository already has staged changes; refusing to mix commit state",
            )

        await self._assert_no_external_filters(policy, normalized)
        add = await self._git(
            policy,
            ("add", "--", *(_literal_pathspec(path) for path in normalized)),
            timeout_seconds=_ACTION_TIMEOUT_SECONDS,
        )
        if add.returncode != 0:
            await self._unstage_best_effort(policy, normalized)
            raise BridgeError("REPO_STAGE_FAILED", "git add failed")

        staged = await self._staged_paths(policy)
        if not staged:
            raise BridgeError("REPO_NO_CHANGES", "requested paths contain no changes to commit")
        requested = set(normalized)
        unexpected = [item for item in staged if item not in requested]
        if unexpected:
            await self._unstage_best_effort(policy, normalized)
            raise BridgeError(
                "REPO_STAGE_SCOPE_MISMATCH",
                "staging produced changes outside the explicit path set",
            )

        commit = await self._git(
            policy,
            (
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-m",
                message,
            ),
            timeout_seconds=_ACTION_TIMEOUT_SECONDS,
        )
        if commit.returncode != 0:
            await self._unstage_best_effort(policy, normalized)
            raise BridgeError("REPO_COMMIT_FAILED", "git commit failed")
        head = await self._git(
            policy,
            ("rev-parse", "HEAD"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if head.returncode != 0:
            raise BridgeError("REPO_COMMIT_FAILED", "commit succeeded but HEAD could not be read")
        return {
            "commit": head.stdout.strip(),
            "paths": normalized,
            "summary": commit.stdout.strip(),
        }

    async def push(self, policy: WorkdirAgentPolicy) -> dict[str, Any]:
        self._require_enabled(policy)
        if policy.read_only:
            raise BridgeError("WORKDIR_READ_ONLY", "repository push requires a writable workdir")
        await self._require_repo_root(policy)
        remote, merge_ref = await self._push_target(policy)
        result = await self._git(
            policy,
            (
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.sshCommand=ssh",
                "-c",
                "core.askPass=",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.ssh.allow=always",
                "-c",
                "protocol.file.allow=always",
                "-c",
                f"remote.{remote}.mirror=false",
                "-c",
                "push.followTags=false",
                "-c",
                "push.recurseSubmodules=no",
                "-c",
                "push.gpgSign=false",
                "push",
                "--porcelain",
                "--no-verify",
                remote,
                f"HEAD:{merge_ref}",
            ),
            timeout_seconds=_PUSH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise BridgeError("REPO_PUSH_FAILED", "git push failed")
        head = await self._git(
            policy,
            ("rev-parse", "HEAD"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if head.returncode != 0:
            raise BridgeError("REPO_PUSH_FAILED", "push succeeded but HEAD could not be read")
        return {"pushed": True, "commit": head.stdout.strip()}

    @staticmethod
    def _require_enabled(policy: WorkdirAgentPolicy) -> None:
        if not policy.repo_ops_enabled:
            raise BridgeError("REPO_OPS_DISABLED", "Repo Ops is disabled for this workdir")

    @staticmethod
    def _verify_action_integrity(policy: WorkdirAgentPolicy, action: RepoActionPolicy) -> None:
        root = policy.host_path.resolve(strict=True)
        for relative_path, expected_digest in action.integrity:
            normalized = normalize_repo_path(relative_path)
            _validate_host_path(root, normalized, require_existing_file=True)
            candidate = root.joinpath(*PurePosixPath(normalized).parts)
            digest = hashlib.sha256()
            total = 0
            try:
                with candidate.open("rb") as handle:
                    while True:
                        chunk = handle.read(65_536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_INTEGRITY_FILE_BYTES:
                            raise BridgeError(
                                "REPO_ACTION_INTEGRITY_FAILED",
                                "repo action integrity input exceeds its size limit",
                            )
                        digest.update(chunk)
            except BridgeError:
                raise
            except OSError as exc:
                raise BridgeError(
                    "REPO_ACTION_INTEGRITY_FAILED",
                    "repo action integrity input could not be read",
                ) from exc
            if not hmac.compare_digest(digest.hexdigest(), expected_digest):
                raise BridgeError(
                    "REPO_ACTION_INTEGRITY_FAILED",
                    "repo action integrity verification failed",
                )

    @staticmethod
    def _action(policy: WorkdirAgentPolicy, name: str) -> RepoActionPolicy:
        if not isinstance(name, str) or not name:
            raise BridgeError("INVALID_REQUEST", "repo action name must be non-empty")
        for action in policy.repo_actions:
            if action.name == name:
                return action
        raise BridgeError("REPO_ACTION_NOT_ALLOWED", "repo action is not allowlisted")

    @staticmethod
    def _normalize_paths(paths: list[str], *, require_nonempty: bool = False) -> list[str]:
        if not isinstance(paths, list) or len(paths) > _MAX_PATHS:
            raise BridgeError("INVALID_REQUEST", f"paths must contain at most {_MAX_PATHS} items")
        normalized = [normalize_repo_path(path) for path in paths]
        if require_nonempty and not normalized:
            raise BridgeError("INVALID_REQUEST", "at least one repo path is required")
        if len(normalized) != len(set(normalized)):
            raise BridgeError("INVALID_REQUEST", "repo paths must be unique")
        return normalized

    @staticmethod
    def _validate_commit_message(message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise BridgeError("INVALID_REQUEST", "commit message must be non-empty")
        if any(char in message for char in ("\x00", "\n", "\r")):
            raise BridgeError("INVALID_REQUEST", "commit message must be one line")
        if len(message.encode("utf-8")) > _MAX_COMMIT_MESSAGE_BYTES:
            raise BridgeError("INVALID_REQUEST", "commit message exceeds its size limit")

    async def _require_repo_root(self, policy: WorkdirAgentPolicy) -> None:
        top = await self._git(
            policy,
            ("rev-parse", "--show-toplevel"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        git_dir = await self._git(
            policy,
            ("rev-parse", "--absolute-git-dir"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if top.returncode != 0 or git_dir.returncode != 0:
            raise BridgeError("NOT_A_GIT_REPOSITORY", "workdir root is not a Git repository")
        try:
            root = policy.host_path.resolve(strict=True)
            top_path = Path(top.stdout.strip()).resolve(strict=True)
            git_path = Path(git_dir.stdout.strip()).resolve(strict=True)
            if top_path != root:
                raise ValueError
            git_path.relative_to(root)
        except (OSError, ValueError) as exc:
            raise BridgeError(
                "REPO_ROOT_MISMATCH",
                "Git repository metadata is not confined to the configured workdir",
            ) from exc

    async def _git(
        self,
        policy: WorkdirAgentPolicy,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        return await run_bounded_command(
            ("git", "-c", "core.fsmonitor=false", *args),
            cwd=policy.host_path,
            timeout_seconds=timeout_seconds,
            git_environment=True,
            strict_utf8=True,
        )

    async def _push_target(self, policy: WorkdirAgentPolicy) -> tuple[str, str]:
        """Resolve one exact current-branch upstream target from operator-owned Git config."""
        branch = await self._git(
            policy,
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if branch.returncode != 0 or not branch.stdout.strip():
            raise BridgeError("REPO_PUSH_FAILED", "current branch could not be resolved")
        branch_name = branch.stdout.strip()
        remote = await self._git(
            policy,
            ("config", "--get", f"branch.{branch_name}.remote"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        merge = await self._git(
            policy,
            ("config", "--get", f"branch.{branch_name}.merge"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if remote.returncode != 0 or merge.returncode != 0:
            raise BridgeError("REPO_PUSH_FAILED", "current branch has no configured upstream")
        remote_name = remote.stdout.strip()
        merge_ref = merge.stdout.strip()
        if remote_name == "." or _REMOTE_NAME_RE.fullmatch(remote_name) is None:
            raise BridgeError("REPO_PUSH_FAILED", "configured upstream remote name is invalid")
        if not merge_ref.startswith("refs/heads/"):
            raise BridgeError("REPO_PUSH_FAILED", "configured upstream ref is not a branch")
        checked = await self._git(
            policy,
            ("check-ref-format", merge_ref),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if checked.returncode != 0:
            raise BridgeError("REPO_PUSH_FAILED", "configured upstream branch ref is invalid")
        return remote_name, merge_ref

    async def _assert_no_external_filters(
        self, policy: WorkdirAgentPolicy, paths: list[str]
    ) -> None:
        """Refuse staging paths whose Git attributes select an external clean/process filter."""
        result = await self._git(
            policy,
            (
                "check-attr",
                "-z",
                "filter",
                "--",
                *paths,
            ),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise BridgeError("REPO_STAGE_FAILED", "could not inspect Git filter attributes")
        fields = _nul_paths(result.stdout)
        if len(fields) != len(paths) * 3:
            raise BridgeError("REPO_STAGE_FAILED", "Git filter attribute response was malformed")
        for index in range(0, len(fields), 3):
            returned_path, attribute, value = fields[index : index + 3]
            expected_path = paths[index // 3]
            if returned_path != expected_path or attribute != "filter":
                raise BridgeError(
                    "REPO_STAGE_FAILED", "Git filter attribute response was malformed"
                )
            if value not in {"unspecified", "unset"}:
                raise BridgeError(
                    "REPO_EXTERNAL_FILTER_NOT_ALLOWED",
                    "requested path selects a Git content filter; refusing deterministic staging",
                )

    async def _staged_paths(self, policy: WorkdirAgentPolicy) -> list[str]:
        result = await self._git(
            policy,
            ("diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv"),
            timeout_seconds=_READ_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise BridgeError("REPO_STATUS_FAILED", "could not inspect staged changes")
        return [item for item in result.stdout.split("\x00") if item]

    async def _unstage_best_effort(self, policy: WorkdirAgentPolicy, paths: list[str]) -> None:
        try:
            await self._git(
                policy,
                (
                    "reset",
                    "-q",
                    "HEAD",
                    "--",
                    *(_literal_pathspec(path) for path in paths),
                ),
                timeout_seconds=_READ_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
