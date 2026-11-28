"""Real Git-for-Windows semantics in disposable repositories, never the live remote."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import RepoActionPolicy, WorkdirAgentPolicy
from serverfs_agent_bridge.repo_ops import RepoOpsRunner, _command_env

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("git") is None,
    reason="real Git-for-Windows semantics",
)


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    input: str | None = None,
    env: dict[str, str] | None = None,
):
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        env=_command_env(git=True) if env is None else env,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class RecordingRunner(RepoOpsRunner):
    def __init__(self):
        self.calls = []

    async def _git(self, policy, args, *, timeout_seconds):
        self.calls.append(args)
        return await super()._git(policy, args, timeout_seconds=timeout_seconds)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch):
    # Configuration and credential discovery never access the developer's real profile.
    profile = tmp_path / "user profile"
    profile.mkdir()
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(profile / "xdg"))
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Windows Repo Ops Test")
    git(repo, "config", "user.email", "repo-ops@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "base.txt").write_bytes(b"base\n")
    (repo / "notes").mkdir()
    (repo / "notes/记录 space.md").write_bytes(b"original\n")
    git(repo, "add", "--", "base.txt", "notes/记录 space.md")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "Initial")
    policy = WorkdirAgentPolicy(
        slot=None,
        alias="repo",
        host_path=repo,
        mode=AgentMode.DISABLED,
        runtimes=frozenset(),
        read_only=False,
        repo_ops_enabled=True,
    )
    return repo, policy, RecordingRunner()


def python_helper(script: Path) -> str:
    # Operator-owned Git configuration inside a disposable test repository.
    return " ".join(shlex.quote(path.as_posix()) for path in (Path(sys.executable), script))


@pytest.mark.asyncio
async def test_userprofile_only_discovers_global_config_and_credential_helper(
    repository, tmp_path: Path, monkeypatch
) -> None:
    repo, policy, runner = repository
    profile = Path(os.environ["USERPROFILE"])
    helper = tmp_path / "credential helper.py"
    marker = profile / "helper-used"
    helper.write_text(
        "import os,sys\nfrom pathlib import Path\nsys.stdin.read()\n"
        "Path(os.environ['USERPROFILE'], 'helper-used').write_text('used')\n"
        "print('username=serverfs-test')\nprint('password=disposable-test-value')\n",
        encoding="utf-8",
    )
    configured_helper = "!" + python_helper(helper)
    git(repo, "config", "--global", "serverfs-test.context", "isolated-profile")
    git(repo, "config", "--global", "credential.helper", configured_helper)
    monkeypatch.delenv("HOME")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    env = _command_env(git=True)
    assert env["USERPROFILE"] == str(profile)
    assert "HOME" not in env
    assert (
        not {"HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PATHEXT"}
        & env.keys()
    )
    context = await runner._git(
        policy, ("config", "--global", "--get", "serverfs-test.context"), timeout_seconds=10
    )
    credential = await runner._git(
        policy, ("config", "--global", "--get", "credential.helper"), timeout_seconds=10
    )
    assert context.returncode == 0 and context.stdout.strip() == "isolated-profile"
    assert credential.returncode == 0 and credential.stdout.strip() == configured_helper
    # `credential fill` invokes a local helper without any network request or real credential.
    result = git(repo, "credential", "fill", input="protocol=https\nhost=example.invalid\n\n")
    assert "username=serverfs-test" in result.stdout
    assert "password=disposable-test-value" in result.stdout
    assert marker.read_text() == "used"


@pytest.mark.asyncio
async def test_explicit_home_keeps_precedence_over_userprofile(
    repository, tmp_path: Path, monkeypatch
) -> None:
    repo, policy, runner = repository
    profile = tmp_path / "other profile"
    profile.mkdir()
    (profile / ".gitconfig").write_text("[serverfs-test]\ncontext = wrong-profile\n")
    git(repo, "config", "--global", "serverfs-test.context", "explicit-home")
    monkeypatch.setenv("USERPROFILE", str(profile))
    result = await runner._git(
        policy, ("config", "--global", "--get", "serverfs-test.context"), timeout_seconds=10
    )
    assert result.returncode == 0 and result.stdout.strip() == "explicit-home"


def test_windows_git_context_does_not_forward_secrets_or_ambient_git_overrides(monkeypatch) -> None:
    keys = (
        "SERVERFS_TEST_SECRET",
        "GH_TOKEN",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "PATHEXT",
    )
    for key in keys:
        monkeypatch.setenv(key, "must-not-forward")
    git_env = _command_env(git=True)
    action_env = _command_env(git=False)
    for key in keys:
        assert key not in git_env and key not in action_env
    assert "USERPROFILE" not in action_env
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    assert git_env["GIT_ALLOW_PROTOCOL"] == "https:ssh:file"


@pytest.mark.asyncio
async def test_status_and_explicit_unstaged_and_staged_diff(repository) -> None:
    repo, policy, runner = repository
    requested = "notes/记录 space.md"
    (repo / requested).write_bytes(b"staged change\n")
    git(repo, "add", "--", requested)
    (repo / requested).write_bytes(b"unstaged change\n")
    (repo / "base.txt").write_bytes(b"unrequested change\n")
    (repo / "untracked name.txt").write_bytes(b"untracked\n")
    result = await runner.inspect(policy, view="status", paths=[])
    assert result["head"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result["branch"] == "main" and result["upstream"] is None
    assert set(result["unstaged_paths"]) == {"base.txt", requested}
    assert result["staged_paths"] == [requested]
    assert result["untracked_paths"] == ["untracked name.txt"]
    unstaged = await runner.inspect(policy, view="diff", paths=[requested])
    staged = await runner.inspect(policy, view="staged_diff", paths=[requested])
    assert "+unstaged change" in unstaged["diff"]
    assert "+staged change" in staged["diff"]
    assert "unrequested change" not in unstaged["diff"] + staged["diff"]
    assert any(f":(literal){requested}" in call for call in runner.calls)
    with pytest.raises(BridgeError, match="explicit path"):
        await runner.inspect(policy, view="diff", paths=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["none", "path", "basename"])
async def test_fixed_pinned_action_and_integrity_refusal(repository, argument: str) -> None:
    repo, policy, runner = repository
    script = repo / "check.py"
    script.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    action = RepoActionPolicy(
        name="check",
        argv=(sys.executable, "check.py", "fixed"),
        argument=argument,
        integrity=(("check.py", hashlib.sha256(script.read_bytes()).hexdigest()),),
    )
    policy = replace(policy, repo_actions=(action,))
    path = None if argument == "none" else "notes/记录 space.md"
    result = await runner.run_action(policy, action_name="check", path=path)
    expected = ["fixed"] + (
        [] if path is None else [Path(path).name if argument == "basename" else path]
    )
    assert result["returncode"] == 0
    assert json.loads(result["stdout"]) == expected
    script.write_text("raise SystemExit('changed pin must not execute')\n", encoding="utf-8")
    with pytest.raises(BridgeError) as exc:
        await runner.run_action(policy, action_name="check", path=path)
    assert exc.value.code == "REPO_ACTION_INTEGRITY_FAILED"


@pytest.mark.asyncio
async def test_commit_literal_staging_scope_and_unrequested_changes(repository) -> None:
    repo, policy, runner = repository
    literal = "notes/[draft].md"
    other = "notes/d.md"
    for name in (literal, other):
        (repo / name).write_bytes(b"original\n")
    git(repo, "add", "--", f":(literal){literal}", other)
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "Add paths")
    for name in (literal, other, "base.txt"):
        (repo / name).write_bytes(b"changed\n")
    result = await runner.commit(policy, paths=[literal], message="Only literal draft")
    assert result["commit"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout.splitlines() == [literal]
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""
    assert set(git(repo, "diff", "--name-only").stdout.splitlines()) == {other, "base.txt"}
    assert any(
        call[:2] == ("add", "--") and call[2:] == (f":(literal){literal}",) for call in runner.calls
    )


@pytest.mark.asyncio
async def test_commit_refuses_existing_staged_state(repository) -> None:
    repo, policy, runner = repository
    before = git(repo, "rev-parse", "HEAD").stdout
    (repo / "notes/记录 space.md").write_bytes(b"pre-staged\n")
    git(repo, "add", "--", "notes/记录 space.md")
    (repo / "base.txt").write_bytes(b"requested\n")
    with pytest.raises(BridgeError) as exc:
        await runner.commit(policy, paths=["base.txt"], message="Refuse dirty index")
    assert exc.value.code == "REPO_INDEX_DIRTY"
    assert git(repo, "rev-parse", "HEAD").stdout == before
    assert not any(call[0] in {"add", "commit"} for call in runner.calls)


@pytest.mark.asyncio
async def test_dev_null_disables_real_windows_hooks_and_commit_signing(repository) -> None:
    repo, policy, runner = repository
    hooks = repo / "operator hooks"
    hooks.mkdir()
    marker = repo / "hook-ran"
    (hooks / "pre-commit").write_bytes(b"#!/bin/sh\nprintf ran > hook-ran\nexit 1\n")
    git(repo, "config", "core.hooksPath", hooks.as_posix())
    control = git(
        repo,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--allow-empty",
        "-m",
        "Hook control",
        check=False,
    )
    assert control.returncode != 0 and marker.exists()
    marker.unlink()
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", (repo / "missing-signing-tool").as_posix())
    signing = git(
        repo,
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "Signing control",
        check=False,
    )
    assert signing.returncode != 0
    assert "gpg" in signing.stderr.lower() or "sign" in signing.stderr.lower()
    assert not marker.exists()
    (repo / "base.txt").write_bytes(b"unsigned and hook-free\n")
    await runner.commit(policy, paths=["base.txt"], message="Hardened commit")
    assert not marker.exists()
    assert "gpgsig " not in git(repo, "cat-file", "-p", "HEAD").stdout


@pytest.mark.asyncio
async def test_commit_refuses_an_active_external_clean_filter(repository) -> None:
    repo, policy, runner = repository
    script = repo / "filter.py"
    marker = repo / "filter-ran"
    script.write_text(
        "import sys\nfrom pathlib import Path\nPath('filter-ran').write_text('ran')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    (repo / ".gitattributes").write_text("base.txt filter=external\n", encoding="utf-8")
    git(repo, "config", "filter.external.clean", python_helper(script))
    git(repo, "hash-object", "--path=base.txt", "base.txt")
    assert marker.exists()
    marker.unlink()
    (repo / "base.txt").write_bytes(b"changed\n")
    with pytest.raises(BridgeError) as exc:
        await runner.commit(policy, paths=["base.txt"], message="Refuse filter")
    assert exc.value.code == "REPO_EXTERNAL_FILTER_NOT_ALLOWED"
    assert not marker.exists()
    assert not any(call[0] == "add" for call in runner.calls)


@pytest.mark.asyncio
async def test_diff_does_not_run_external_diff_or_textconv(repository) -> None:
    repo, policy, runner = repository
    script = repo / "diff-helper.py"
    marker = repo / "diff-helper-ran"
    script.write_text("from pathlib import Path\nPath('diff-helper-ran').write_text('ran')\n")
    command = python_helper(script)
    git(repo, "config", "diff.external", command)
    git(repo, "config", "diff.externaldriver.textconv", command)
    (repo / ".gitattributes").write_text("base.txt diff=externaldriver\n")
    (repo / "base.txt").write_bytes(b"changed\n")
    # The deliberately enabled control needs temporary files. Keep them in the workdir;
    # the production invocation below still uses the unmodified narrow environment.
    control_temp = repo / "textconv temp"
    control_temp.mkdir()
    control_env = {**_command_env(git=True), "TMPDIR": control_temp.as_posix()}
    git(repo, "diff", "--no-ext-diff", "--textconv", "--", "base.txt", env=control_env)
    assert marker.exists()
    marker.unlink()
    result = await runner.inspect(policy, view="diff", paths=["base.txt"])
    assert "+changed" in result["diff"]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_push_derives_non_origin_upstream_and_only_exact_head_refspec(
    repository, tmp_path: Path
) -> None:
    repo, policy, runner = repository
    remote = tmp_path / "local file remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "depot", remote.as_uri())
    git(repo, "push", "--set-upstream", "depot", "HEAD:refs/heads/integration")
    initial = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "other")
    git(repo, "push", "depot", "refs/heads/other:refs/heads/other")
    (repo / "base.txt").write_bytes(b"next\n")
    committed = await runner.commit(policy, paths=["base.txt"], message="Next")
    git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "extra-tag", "-m", "must stay local")
    git(repo, "config", "remote.depot.push", "refs/heads/other:refs/heads/wrong-target")
    git(repo, "config", "remote.depot.mirror", "true")
    git(repo, "config", "push.followTags", "true")
    git(repo, "config", "push.recurseSubmodules", "on-demand")
    git(repo, "config", "push.gpgSign", "true")
    hooks = repo / "push hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_bytes(b"#!/bin/sh\nprintf ran > push-hook-ran\nexit 1\n")
    git(repo, "config", "core.hooksPath", hooks.as_posix())
    control = git(
        repo,
        "-c",
        "remote.depot.mirror=false",
        "-c",
        "push.gpgSign=false",
        "push",
        "depot",
        "HEAD:refs/heads/integration",
        check=False,
    )
    marker = repo / "push-hook-ran"
    assert control.returncode != 0 and marker.exists()
    marker.unlink()
    result = await runner.push(policy)
    assert result == {"pushed": True, "commit": committed["commit"]}
    pushes = [call for call in runner.calls if "push" in call]
    assert len(pushes) == 1
    assert pushes[0][-2:] == ("depot", "HEAD:refs/heads/integration")
    assert not {"--force", "--all", "--mirror", "--tags"} & set(pushes[0])
    assert not any(arg.startswith("+") for arg in pushes[0])
    refs = dict(
        line.split()
        for line in git(
            remote, "for-each-ref", "--format=%(refname) %(objectname)"
        ).stdout.splitlines()
    )
    assert refs == {"refs/heads/integration": committed["commit"], "refs/heads/other": initial}
    assert not marker.exists()
    status = await runner.inspect(policy, view="status", paths=[])
    assert status["upstream"] == "depot/integration"


@pytest.mark.asyncio
async def test_push_blocks_custom_remote_helper_even_if_repository_allows_it(repository) -> None:
    repo, policy, runner = repository
    git(repo, "remote", "add", "origin", "ext::must-not-run")
    git(repo, "config", "protocol.ext.allow", "always")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "branch", "--set-upstream-to=origin/main", "main")
    with pytest.raises(BridgeError) as exc:
        await runner.push(policy)
    assert exc.value.code == "REPO_PUSH_FAILED"
