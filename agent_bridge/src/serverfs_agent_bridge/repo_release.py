"""Built-in, operator-enabled release actions; no caller-selected Git/GitHub commands."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .errors import BridgeError
from .policy import RepoActionPolicy, WorkdirAgentPolicy
from .repo_ops import run_bounded_command

if TYPE_CHECKING:
    from .repo_ops import RepoOpsRunner

# Keep all transport overrides and hooks out of deterministic release children.
_HARDENED = (
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
    "remote.origin.mirror=false",
    "-c",
    "push.followTags=false",
    "-c",
    "push.recurseSubmodules=no",
    "-c",
    "push.gpgSign=false",
)


async def run_release_action(
    runner: RepoOpsRunner,
    policy: WorkdirAgentPolicy,
    action: RepoActionPolicy,
    tag: str,
) -> dict[str, Any]:
    """Execute under the caller's existing exclusive lease and verified integrity pins."""
    if policy.read_only:
        raise BridgeError("WORKDIR_READ_ONLY", "release requires a writable workdir")

    async def git(*args: str, missing_ok: bool = False):
        result = await runner._git(policy, (*_HARDENED, *args), timeout_seconds=120)
        if result.returncode != 0 and not (missing_ok and result.returncode == 1):
            raise BridgeError("REPO_RELEASE_FAILED", "release Git check or operation failed")
        return result

    branch = (await git("symbolic-ref", "--quiet", "HEAD")).stdout.strip()
    if branch != "refs/heads/main" or await runner._push_target(policy) != (
        "origin",
        "refs/heads/main",
    ):
        raise BridgeError("REPO_RELEASE_STATE", "release requires main tracking origin/main")
    if (await git("status", "--porcelain", "--untracked-files=normal")).stdout:
        raise BridgeError("REPO_RELEASE_STATE", "release requires a clean working tree and index")
    head = (await git("rev-parse", "--verify", "HEAD^{commit}")).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
        raise BridgeError("REPO_RELEASE_STATE", "invalid repository HEAD")
    try:
        package = json.loads((await git("show", "HEAD:package.json")).stdout)
        if package["version"] != tag[1:]:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(
            "REPO_RELEASE_VERSION", "tag must match HEAD package.json version"
        ) from exc

    # Verify both fetch and push destinations after Git URL rewriting; reject multiple URLs.
    repository = action.repository
    allowed_urls = {
        f"https://github.com/{repository}.git",
        f"https://github.com/{repository}",
        f"git@github.com:{repository}.git",
        f"ssh://git@github.com/{repository}.git",
    }
    for options in (("--all",), ("--push", "--all")):
        urls = (await git("remote", "get-url", *options, "origin")).stdout.splitlines()
        if len(urls) != 1 or urls[0] not in allowed_urls:
            raise BridgeError(
                "REPO_RELEASE_REMOTE", "origin must match the fixed GitHub repository"
            )

    ref = f"refs/tags/{tag}"

    async def remote_refs() -> dict[str, str]:
        output = (await git("ls-remote", "origin", "refs/heads/main", ref, ref + "^{}")).stdout
        refs: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if (
                len(fields) != 2
                or fields[1] not in {"refs/heads/main", ref, ref + "^{}"}
                or fields[1] in refs
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]) is None
            ):
                raise BridgeError("REPO_RELEASE_REMOTE", "invalid remote ref evidence")
            refs[fields[1]] = fields[0]
        if refs.get("refs/heads/main") != head:
            raise BridgeError("REPO_RELEASE_STATE", "remote main must equal current HEAD")
        if ref in refs and refs.get(ref + "^{}", refs[ref]) != head:
            raise BridgeError("REPO_TAG_CONFLICT", "remote tag points to another commit")
        return refs

    refs = await remote_refs()
    local = await git("show-ref", "--verify", "--quiet", ref, missing_ok=True)
    if local.returncode == 0:
        peeled = (await git("rev-parse", "--verify", ref + "^{commit}")).stdout.strip()
        if peeled != head:
            raise BridgeError("REPO_TAG_CONFLICT", "local tag points to another commit")

    if action.kind == "release_tag":
        if local.returncode == 1:
            # Compare-and-create: an existing ref can never be replaced, even by a race.
            await git("update-ref", ref, head, "0" * len(head))
        if ref not in refs:
            # The source is the captured HEAD object, never a caller SHA or a mutable ref.
            await git("push", "--porcelain", "--no-verify", "origin", f"{head}:{ref}")
        verified = await remote_refs()
        if verified.get(ref + "^{}", verified.get(ref)) != head:
            raise BridgeError("REPO_RELEASE_FAILED", "remote tag was not verified")
        return {
            "action": action.name,
            "mutates": True,
            "release_tag": tag,
            "commit": head,
            "pushed": True,
            "already_present": ref in refs,
        }

    if ref not in refs:
        raise BridgeError("REPO_TAG_MISSING", "publish requires an existing remote release tag")
    # Fixed repository and workflow; no inputs, API URL, token, npm invocation or shell.
    result = await run_bounded_command(
        (
            action.github_cli,
            "workflow",
            "run",
            "publish.yml",
            "--repo",
            str(repository),
            "--ref",
            tag,
        ),
        cwd=policy.host_path,
        timeout_seconds=120,
    )
    if result.returncode != 0:
        raise BridgeError("REPO_PUBLISH_DISPATCH_FAILED", "fixed publish workflow dispatch failed")
    return {
        "action": action.name,
        "mutates": True,
        "release_tag": tag,
        "commit": head,
        "workflow": "publish.yml",
        "dispatched": True,
        "dispatch_idempotent": False,
    }
