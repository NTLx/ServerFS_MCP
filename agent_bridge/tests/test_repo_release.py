"""Release boundaries tested against disposable Git remotes; never a live repository."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.policy import RepoActionPolicy
from serverfs_agent_bridge.repo_ops import CommandResult, RepoOpsRunner
from test_repo_ops import git, initialized_repo, policy_for

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only release actions")

TAG = "v1.1.0-beta.3"
REPOSITORY = "example/project"


class LocalRunner(RepoOpsRunner):
    """Allow local file transport only inside this fixture, retaining real Git semantics."""

    def __init__(self):
        self.calls = []
        self.url = f"https://github.com/{REPOSITORY}.git"

    async def _git(self, policy, args, *, timeout_seconds):
        self.calls.append(args)
        if "get-url" in args:
            return CommandResult(0, self.url + "\n", "")
        return await super()._git(
            policy,
            ("-c", "protocol.file.allow=always", *args),
            timeout_seconds=timeout_seconds,
        )


@pytest.fixture
def release_repo(tmp_path):
    repo = initialized_repo(tmp_path)
    (repo / "package.json").write_text(json.dumps({"version": TAG[1:]}))
    workflow = repo / ".github/workflows/publish.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Publish to npm\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Release candidate")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    pins = tuple(
        (p, hashlib.sha256((repo / p).read_bytes()).hexdigest())
        for p in ("package.json", ".github/workflows/publish.yml")
    )
    tag = RepoActionPolicy(
        name="tag_release",
        kind="release_tag",
        mutates=True,
        argument="release_tag",
        repository=REPOSITORY,
        integrity=pins,
    )
    publish = replace(tag, name="publish_npm", kind="publish_npm")
    return repo, remote, policy_for(repo, actions=(tag, publish)), LocalRunner()


async def run(fixture, action="tag_release", tag=TAG, **kwargs):
    _, _, policy, runner = fixture
    return await runner.run_action(policy, action_name=action, path=None, release_tag=tag, **kwargs)


@pytest.mark.asyncio
async def test_create_exact_head_tag_and_idempotency(release_repo):
    repo, remote, _, runner = release_repo
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    result = await run(release_repo)
    assert result["pushed"] and result["commit"] == head
    assert git(remote, "rev-parse", f"refs/tags/{TAG}").stdout.strip() == head
    assert (await run(release_repo))["already_present"]
    pushes = [args for args in runner.calls if "push" in args]
    assert len(pushes) == 1
    assert pushes[0][-2:] == ("origin", f"{head}:refs/tags/{TAG}")
    assert not any(arg.startswith("+") or arg == "--force" for arg in pushes[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "../../xxx",
        "main",
        "refs/heads/main",
        "v1.2.3 --force",
        "v1.2.3;id",
        "v1.2.3$(id)",
        "v01.2.3",
        "v1.2.3\n",
        "v1.2.3-rc.1",
        "v" + "1" * 130,
    ],
)
async def test_reject_bad_tags_before_any_git(release_repo, tag):
    with pytest.raises(BridgeError, match="semver"):
        await run(release_repo, tag=tag)
    assert release_repo[3].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["local", "remote"])
async def test_conflicting_tag_never_replaced(release_repo, location):
    repo, remote, _, _ = release_repo
    previous = git(repo, "rev-parse", "HEAD~1").stdout.strip()
    git(repo, "tag", TAG, previous)
    if location == "remote":
        git(repo, "push", "origin", f"refs/tags/{TAG}")
        git(repo, "tag", "-d", TAG)
    with pytest.raises(BridgeError) as exc:
        await run(release_repo)
    assert exc.value.code == "REPO_TAG_CONFLICT"
    source = remote if location == "remote" else repo
    assert git(source, "rev-parse", f"refs/tags/{TAG}").stdout.strip() == previous


@pytest.mark.asyncio
async def test_annotated_existing_tag_same_head_is_idempotent(release_repo):
    repo, _, _, _ = release_repo
    git(repo, "tag", "-a", TAG, "-m", "Existing")
    git(repo, "push", "origin", f"refs/tags/{TAG}")
    assert (await run(release_repo))["already_present"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "dirty",
        "staged",
        "untracked",
        "head_ahead",
        "wrong_version",
        "wrong_remote",
        "wrong_branch",
        "bad_pin",
    ],
)
async def test_release_preconditions_fail_without_tag(release_repo, state):
    repo, remote, policy, runner = release_repo
    if state == "dirty":
        (repo / "base.txt").write_text("changed")
    elif state == "staged":
        (repo / "base.txt").write_text("changed")
        git(repo, "add", "base.txt")
    elif state == "untracked":
        (repo / "unexpected").touch()
    elif state == "head_ahead":
        git(repo, "commit", "--allow-empty", "-m", "Ahead")
    elif state == "wrong_remote":
        runner.url = "https://github.com/attacker/project.git"
    elif state == "wrong_branch":
        git(repo, "checkout", "-b", "other")
    elif state == "bad_pin":
        action = replace(
            policy.repo_actions[0],
            integrity=(("package.json", "0" * 64), (".github/workflows/publish.yml", "0" * 64)),
        )
        release_repo = (repo, remote, replace(policy, repo_actions=(action,)), runner)
    with pytest.raises(BridgeError):
        await run(release_repo, tag="v9.9.9" if state == "wrong_version" else TAG)
    assert git(repo, "tag", "--list").stdout == ""
    assert git(remote, "tag", "--list").stdout == ""


@pytest.mark.asyncio
async def test_publish_requires_remote_tag_and_dispatches_only_fixed_workflow(
    release_repo, monkeypatch
):
    with pytest.raises(BridgeError) as exc:
        await run(release_repo, action="publish_npm")
    assert exc.value.code == "REPO_TAG_MISSING"
    await run(release_repo)
    calls = []

    async def dispatch(argv, **kwargs):
        calls.append((argv, kwargs))
        return CommandResult(0, "", "")

    monkeypatch.setattr("serverfs_agent_bridge.repo_release.run_bounded_command", dispatch)
    result = await run(release_repo, action="publish_npm")
    assert result["dispatched"] and result["dispatch_idempotent"] is False
    assert calls[0][0] == (
        "gh",
        "workflow",
        "run",
        "publish.yml",
        "--repo",
        REPOSITORY,
        "--ref",
        TAG,
    )
    assert "env" not in calls[0][1]


@pytest.mark.parametrize(
    "change",
    [
        {"argv": ("sh",)},
        {"argument": "path"},
        {"mutates": False},
        {"repository": "../../evil"},
        {"kind": "arbitrary_workflow"},
        {"repository": "--help"},
    ],
)
def test_policy_cannot_become_generic_executor(release_repo, change):
    with pytest.raises(ValueError):
        replace(release_repo[2].repo_actions[0], **change)


@pytest.mark.asyncio
async def test_publish_uses_only_admin_cli_path(release_repo, monkeypatch):
    await run(release_repo)
    repo, remote, policy, runner = release_repo
    publish = replace(policy.repo_actions[1], github_cli="/operator/bin/gh")
    policy = replace(policy, repo_actions=(publish,))
    seen = []

    async def dispatch(argv, **kwargs):
        seen.append(argv)
        return CommandResult(0, "", "")

    monkeypatch.setattr("serverfs_agent_bridge.repo_release.run_bounded_command", dispatch)
    assert (await run((repo, remote, policy, runner), action="publish_npm"))["dispatched"]
    assert seen[0][0] == "/operator/bin/gh"
    with pytest.raises(ValueError):
        replace(publish, github_cli="relative/gh")
