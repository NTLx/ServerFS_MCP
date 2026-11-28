"""Deterministic host Repo Ops: fixed Git/actions, confinement, and no Agent runtime."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import RepoActionPolicy, WorkdirAgentPolicy
from serverfs_agent_bridge.repo_ops import RepoOpsRunner, normalize_repo_path, run_bounded_command

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("git") is None,
    reason="v0.23 Repo Ops live command contract is initially Linux/Git scoped",
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def initialized_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Repo Ops Test")
    git(repo, "config", "user.email", "repo-ops@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "--", "base.txt")
    git(repo, "commit", "-m", "Initial")
    return repo


def policy_for(repo: Path, *, actions: tuple[RepoActionPolicy, ...] = ()) -> WorkdirAgentPolicy:
    return WorkdirAgentPolicy(
        slot=1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.DISABLED,
        runtimes=frozenset(),
        read_only=False,
        repo_ops_enabled=True,
        repo_actions=actions,
    )


def test_normalize_repo_path_rejects_escape_and_platform_path_syntax() -> None:
    assert normalize_repo_path("wiki/topic.md") == "wiki/topic.md"
    for value in ("", "../outside", "/absolute", r"dir\file", "C:/outside", "--option"):
        with pytest.raises(BridgeError) as exc:
            normalize_repo_path(value)
        assert exc.value.code == "INVALID_REPO_PATH"


def test_status_and_diff_are_bounded_fixed_git_operations(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    policy = policy_for(repo)
    runner = RepoOpsRunner()
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")

    status = asyncio.run(runner.inspect(policy, view="status", paths=[]))
    assert status["view"] == "status"
    assert status["branch"] == "main"
    assert status["upstream"] is None
    assert status["unstaged_paths"] == ["base.txt"]
    diff = asyncio.run(runner.inspect(policy, view="diff", paths=["base.txt"]))
    assert diff["view"] == "diff"
    assert "changed" in diff["diff"]


def test_status_fails_closed_for_non_utf8_git_path_output(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    raw_path = os.fsencode(repo) + b"/bad-\xff.txt"
    fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"data")
    finally:
        os.close(fd)

    with pytest.raises(BridgeError) as exc:
        asyncio.run(RepoOpsRunner().inspect(policy_for(repo), view="status", paths=[]))
    assert exc.value.code == "REPO_OUTPUT_ENCODING_INVALID"


def test_diff_rejects_symlink_path(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    (repo / "target.md").write_text("target", encoding="utf-8")
    (repo / "link.md").symlink_to("target.md")
    runner = RepoOpsRunner()
    with pytest.raises(BridgeError) as exc:
        asyncio.run(runner.inspect(policy_for(repo), view="diff", paths=["link.md"]))
    assert exc.value.code == "INVALID_REPO_PATH"


def test_named_action_uses_only_operator_fixed_argv(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    action = RepoActionPolicy(
        name="check",
        argv=(sys.executable, "-c", "print('fixed-action')"),
        mutates=False,
        argument="none",
        integrity=(
            (
                "base.txt",
                "f34848ca92665c342abd5816c9e3eda0e82180671195362bcd0080544a3bc2ac",
            ),
        ),
    )
    runner = RepoOpsRunner()
    result = asyncio.run(
        runner.run_action(
            policy_for(repo, actions=(action,)),
            action_name="check",
            path=None,
        )
    )
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "fixed-action"


def test_named_action_does_not_inherit_ambient_bridge_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = initialized_repo(tmp_path)
    monkeypatch.setenv("SERVERFS_TEST_SECRET", "must-not-leak")
    action = RepoActionPolicy(
        name="envcheck",
        argv=(
            sys.executable,
            "-c",
            "import os; print(os.environ.get('SERVERFS_TEST_SECRET', 'missing'))",
        ),
        mutates=False,
        argument="none",
        integrity=(
            (
                "base.txt",
                "f34848ca92665c342abd5816c9e3eda0e82180671195362bcd0080544a3bc2ac",
            ),
        ),
    )
    result = asyncio.run(
        RepoOpsRunner().run_action(
            policy_for(repo, actions=(action,)),
            action_name="envcheck",
            path=None,
        )
    )
    assert result["stdout"].strip() == "missing"


def test_bounded_command_timeout_kills_child_process_group(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    marker = tmp_path / "escaped-child.txt"
    child_code = (
        "import pathlib,time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )
    with pytest.raises(BridgeError) as exc:
        asyncio.run(
            run_bounded_command(
                (sys.executable, "-c", parent_code),
                cwd=repo,
                timeout_seconds=0.1,
            )
        )
    assert exc.value.code == "REPO_TIMEOUT"
    time.sleep(0.7)
    assert not marker.exists()


def test_timeout_kills_descendant_after_process_group_leader_exits(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    marker = tmp_path / "escaped-orphan.txt"
    child_code = (
        "import pathlib,time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
    )
    # The parent exits immediately. Its child inherits stdout/stderr, so the bounded
    # command remains open until timeout even though process.returncode is already set.
    parent_code = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child_code!r}])"
    with pytest.raises(BridgeError) as exc:
        asyncio.run(
            run_bounded_command(
                (sys.executable, "-c", parent_code),
                cwd=repo,
                timeout_seconds=0.1,
            )
        )
    assert exc.value.code == "REPO_TIMEOUT"
    time.sleep(0.7)
    assert not marker.exists()


def test_path_action_rejects_symlink_and_requires_regular_file(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    target = repo / "source.md"
    target.write_text("source", encoding="utf-8")
    link = repo / "link.md"
    link.symlink_to(target.name)
    action = RepoActionPolicy(
        name="pathcheck",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])"),
        mutates=False,
        argument="path",
        integrity=(
            (
                "base.txt",
                "f34848ca92665c342abd5816c9e3eda0e82180671195362bcd0080544a3bc2ac",
            ),
        ),
    )
    runner = RepoOpsRunner()
    policy = policy_for(repo, actions=(action,))

    result = asyncio.run(runner.run_action(policy, action_name="pathcheck", path="source.md"))
    assert result["stdout"].strip() == "source.md"
    with pytest.raises(BridgeError) as exc:
        asyncio.run(runner.run_action(policy, action_name="pathcheck", path="link.md"))
    assert exc.value.code == "INVALID_REPO_PATH"


def test_basename_action_validates_full_path_but_passes_only_final_name(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    raw = repo / "raw"
    raw.mkdir()
    source = raw / "source.md"
    source.write_text("source", encoding="utf-8")
    action = RepoActionPolicy(
        name="ensure",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])"),
        mutates=True,
        argument="basename",
        integrity=(
            (
                "base.txt",
                "f34848ca92665c342abd5816c9e3eda0e82180671195362bcd0080544a3bc2ac",
            ),
        ),
    )
    result = asyncio.run(
        RepoOpsRunner().run_action(
            policy_for(repo, actions=(action,)),
            action_name="ensure",
            path="raw/source.md",
        )
    )
    assert result["stdout"].strip() == "source.md"


def test_commit_refuses_preexisting_staged_changes(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    policy = policy_for(repo)
    runner = RepoOpsRunner()
    (repo / "pre.txt").write_text("pre", encoding="utf-8")
    git(repo, "add", "--", "pre.txt")
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(BridgeError) as exc:
        asyncio.run(runner.commit(policy, paths=["base.txt"], message="Change base"))
    assert exc.value.code == "REPO_INDEX_DIRTY"


def test_commit_refuses_external_git_clean_filter(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    marker = tmp_path / "filter-ran.txt"
    git(repo, "config", "filter.evil.clean", f"sh -c 'echo ran > {marker}; cat'")
    (repo / ".gitattributes").write_text("base.txt filter=evil\n", encoding="utf-8")
    (repo / "base.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(BridgeError) as exc:
        asyncio.run(RepoOpsRunner().commit(policy_for(repo), paths=["base.txt"], message="Change"))
    assert exc.value.code == "REPO_EXTERNAL_FILTER_NOT_ALLOWED"
    assert not marker.exists()


def test_commit_disables_repository_gpg_signing_configuration(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", "/definitely/missing/serverfs-test-gpg")
    (repo / "base.txt").write_text("signed-config-but-not-command\n", encoding="utf-8")

    result = asyncio.run(
        RepoOpsRunner().commit(policy_for(repo), paths=["base.txt"], message="No signing")
    )
    assert len(result["commit"]) == 40


def test_commit_uses_literal_pathspec_and_leaves_unrequested_change_unstaged(
    tmp_path: Path,
) -> None:
    repo = initialized_repo(tmp_path)
    policy = policy_for(repo)
    runner = RepoOpsRunner()
    literal = repo / "literal*.md"
    other = repo / "other.md"
    literal.write_text("one\n", encoding="utf-8")
    other.write_text("other\n", encoding="utf-8")
    git(repo, "add", "--", "literal*.md", "other.md")
    git(repo, "commit", "-m", "Add markdown")
    literal.write_text("two\n", encoding="utf-8")
    other.write_text("changed\n", encoding="utf-8")

    result = asyncio.run(runner.commit(policy, paths=["literal*.md"], message="Update literal"))
    assert len(result["commit"]) == 40
    status = git(repo, "status", "--porcelain=v1").stdout.splitlines()
    assert status == [" M other.md"]
    changed = git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
    assert changed == ["literal*.md"]


def test_commit_rejects_directory_and_symlink_paths(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    policy = policy_for(repo)
    runner = RepoOpsRunner()
    directory = repo / "wiki"
    directory.mkdir()
    (directory / "topic.md").write_text("topic", encoding="utf-8")
    (repo / "link.md").symlink_to("wiki/topic.md")

    for path in ("wiki", "link.md"):
        with pytest.raises(BridgeError) as exc:
            asyncio.run(runner.commit(policy, paths=[path], message="Unsafe path"))
        assert exc.value.code == "INVALID_REPO_PATH"


def test_push_uses_current_upstream_without_request_remote_or_ref(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    initial = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "other")
    git(repo, "push", "origin", "other")
    git(repo, "tag", "-a", "repo-ops-extra", "-m", "extra")
    git(repo, "config", "remote.origin.push", "refs/heads/other:refs/heads/other")
    git(repo, "config", "remote.origin.mirror", "true")
    git(repo, "config", "push.followTags", "true")
    git(repo, "config", "push.recurseSubmodules", "on-demand")
    git(repo, "config", "push.gpgSign", "true")

    runner = RepoOpsRunner()
    policy = policy_for(repo)
    (repo / "base.txt").write_text("next\n", encoding="utf-8")
    committed = asyncio.run(runner.commit(policy, paths=["base.txt"], message="Next"))
    pushed = asyncio.run(runner.push(policy))
    assert pushed["pushed"] is True
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == committed["commit"]
    remote_other = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/other"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_other == initial
    remote_tag = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--verify", "refs/tags/repo-ops-extra"],
        capture_output=True,
        text=True,
    )
    assert remote_tag.returncode != 0


def test_push_blocks_unapproved_remote_helper_protocol(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    marker = tmp_path / "remote-helper-ran.txt"
    git(repo, "remote", "add", "origin", f"ext::sh -c 'touch {marker}'")
    git(repo, "config", "protocol.ext.allow", "always")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "branch", "--set-upstream-to=origin/main", "main")

    with pytest.raises(BridgeError) as exc:
        asyncio.run(RepoOpsRunner().push(policy_for(repo)))
    assert exc.value.code == "REPO_PUSH_FAILED"
    assert not marker.exists()
