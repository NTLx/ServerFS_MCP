"""I13 two independent Repo-only Windows Core/owned Bridge installer, source-only.

Never creates a repo, installs wheels, starts Scheduler, executes Git, or
reads private credentials when collected. Native --stage is separately gated.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-owned-repo-stage.py"


def module():
    spec = importlib.util.spec_from_file_location("i13_owned_repo_stage_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_exact_two_new_repo_only_instances_and_signed_method_grants() -> None:
    mod = module()
    assert mod.IDS == ("i13-a", "i13-b")
    assert mod.PORTS == {"i13-a": 18883, "i13-b": 18884}
    assert mod.METHODS == (
        "bridge.ping",
        "runtime.list",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
    )
    assert mod.ACTION == "status_probe"
    with pytest.raises(mod.I13StageRefusal):
        mod.require(False, "refuse")


def test_fixed_repo_only_private_core_bridge_policy_matches() -> None:
    mod = module()
    fixture = Path("C:/private-i13/workdirs/fixture")
    import json

    bridge = mod.bridge_policy(
        "i13-a",
        Path("C:/private-i13/services/bridge"),
        fixture,
        "S-1-5-21-1",
        "C:/Git/bin/git.exe",
    )
    core = mod.core_policy(fixture, "C:/Git/bin/git.exe").decode("utf-8")
    entry = bridge["workdirs"][0]
    action = entry["repo_actions"]["status_probe"]
    assert entry["agent_mode"] == "disabled"
    assert entry["agent_runtimes"] == []
    assert entry["repo_ops_enabled"] is True
    assert entry["read_only"] is False
    assert action["argv"] == ["C:/Git/bin/git.exe", "status", "--short"]
    assert action["mutates"] is False and action["argument"] == "none"
    digest = hashlib.sha256(mod.GUARD).hexdigest()
    assert action["integrity"] == {"guard.txt": digest}
    assert f'"guard.txt" = "{digest}"' in core
    assert "repo_ops_enabled = true" in core
    assert 'agent_mode = "disabled"' in core
    assert "agent_runtimes = []" in core
    assert json.dumps(str(fixture)) in core


def test_git_fixture_is_only_new_local_repo_no_remote_no_global_identity() -> None:
    body = SOURCE.read_text("utf-8")
    for required in (
        '"GIT_CONFIG_NOSYSTEM"',
        '"GIT_CONFIG_GLOBAL"',
        '"GIT_ALLOW_PROTOCOL"',
        'env["GIT_ALLOW_PROTOCOL"] = "file"',
        '["init", "--initial-branch=main"]',
        '["config", "--local", "user.name", "ServerFS I13 Test"]',
        '["config", "--local", "user.email", "serverfs-i13@example.invalid"]',
        '["config", "--local", "core.hooksPath", os.devnull]',
        '["add", "--", "guard.txt"]',
        '["commit", "--no-gpg-sign", "-m", "I13 disposable fixture root"]',
        "installer._new_private_file(guard, GUARD, sid)",
        "installer._protect_tree(fixture, sid)",
    ):
        assert required in body, required
    for forbidden in (
        '"clone"',
        '"fetch"',
        '"push"',
        '"remote", "add"',
        "git push",
        "git clone",
        "git fetch",
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "shutil.rmtree(",
        "taskkill",
        "os.system(",
        "shell=True",
    ):
        assert forbidden not in body, forbidden


def test_offline_built_installed_private_worker_and_four_disabled_native_tasks() -> None:
    body = SOURCE.read_text("utf-8")
    for required in (
        "pre.preflight()",
        'storage.prepare("bridge", instance)',
        'storage.prepare("core", instance)',
        "pin.verify_native_wheel(native_wheel)",
        "installer._wheel(",
        "installer._offline_install(",
        "installer.verify_installed_runtime(",
        "installer._protect_tree(",
        "core_policy(fixture, git)",
        "bridge_policy(instance, bridge_home, fixture, sid, git)",
        "core_workdir_policy_digest(policy)",
        "i4._binding_and_grant(instance, digest)",
        'grant["methods"] = list(METHODS)',
        "i3._PROVISION",
        "parse_core_bridge_binding(",
        "load_native_bridge_plan(bridge_manifest)",
        "storage.stage(manifest)",
        "Scheduler().install(manifest)",
        "postflight()",
        "I13_TWO_INDEPENDENT_REPO_ONLY_OWNED_PAIRS_STAGE=PASS",
    ):
        assert required in body, required


def test_postflight_is_exact_offline_private_and_protected_no_agent_runtime() -> None:
    body = SOURCE.read_text("utf-8")
    for required in (
        "old.postflight()",
        "gate.verify_tasks(gate.task_rows(), installed=True)",
        "not workdirs[0].policy.agent_runtimes",
        'workdirs[0].agent_mode == "disabled"',
        "workdirs[0].policy.repo_ops_enabled",
        'binding.bridge_mode == "owned"',
        'status.get("enabled") is False',
        'status.get("running") is False',
        "_current_identity(manifest) is None",
        "guard.read_bytes() == GUARD",
        "storage.check_private(",
        "I13_TWO_PRIVATE_REPO_GUARDS_AND_OWNED_WORKERS=PASS",
        "I13_FOUR_DISABLED_REPO_TASK_OWNERS_AND_GRANTS=PASS",
        "I13_PRODUCTION_AND_I4_I11=UNCHANGED",
    ):
        assert required in body, required


def test_actual_native_repo_actions_live_on_settings_not_workdir_policy(tmp_path) -> None:
    from serverfs_mcp.native_config import load_native_config

    mod = module()
    fixture = tmp_path / "private-fixture"
    fixture.mkdir()
    toml = tmp_path / "serverfs.toml"
    toml.write_bytes(mod.core_policy(fixture, "C:/Git/bin/git.exe"))
    workdirs, settings = load_native_config(toml)
    assert len(workdirs) == 1
    assert workdirs[0].policy.repo_ops_enabled is True
    assert settings.bridge_required is True
    assert settings.repo_ops_enabled is True
    assert len(settings.repo_actions["Fixture"]) == 1
    assert settings.repo_actions["Fixture"][0].name == "status_probe"
    assert not hasattr(workdirs[0].policy, "repo_actions")


def test_one_use_resume_is_fail_closed_exact_partial_installer_checkpoint() -> None:
    source = SOURCE.read_text("utf-8")
    for expected in (
        'action.add_argument("--resume-audit", action="store_true")',
        'action.add_argument("--resume-after-i13-a-core-policy", action="store_true")',
        "def resume_audit() -> None:",
        "resume_audit()",
        "pre.verify_tasks(pre.task_rows(), installed=False)",
        "core_config.read_bytes() == core_policy(a_fixture, git)",
        "bridge_config.read_bytes() == _json(",
        'and len(settings.repo_actions.get("Fixture", ())) == 1',
        'not (home / "service.json").exists()',
        'not (storage.home("core", instance) / "http.env").exists()',
        'not (storage.home("core", instance) / "bridge-binding.json").exists()',
        'not (storage.home("bridge", instance) / "state/core-grants.json").exists()',
        'not (b / "envs" / component / "Scripts/python.exe").exists()',
        'root / "envs/bridge/Scripts/python.exe"',
        "installer.verify_installed_runtime(root, bridge_python, worker_python)",
        "storage.check_private(config, directory=False, sid=sid)",
        "config.read_bytes() == _json(bridge_doc)",
        "storage.check_private(toml, directory=False, sid=sid)",
        "toml.read_bytes() == core_policy(fixture, git)",
        '"repo-resume.attempted"',
        "I13_ONE_USE_RESUME_READONLY_AUDIT=PASS",
        "I13_EXACT_PARTIAL_RESUME_MARKER=SEALED",
    ):
        assert expected in source, expected
    assert "len(workdirs[0].policy.repo_actions)" not in source
    tree = ast.parse(source)
    stage_fn = next(
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "stage"
    )
    resume_calls = [
        node.lineno
        for node in ast.walk(stage_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resume_audit"
    ]
    marker_literals = [
        node.lineno
        for node in ast.walk(stage_fn)
        if isinstance(node, ast.Constant) and node.value == "repo-resume.attempted"
    ]
    assert len(resume_calls) == len(marker_literals) == 1
    assert resume_calls[0] < marker_literals[0]


def test_cli_only_stage_or_readonly_postflight_with_native_windows_identity() -> None:
    body = SOURCE.read_text("utf-8")
    tree = ast.parse(body)
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"native", "fresh_git", "stage", "postflight", "main"} <= functions
    assert 'action.add_argument("--stage", action="store_true")' in body
    assert 'action.add_argument("--postflight", action="store_true")' in body
    assert "not ctypes.windll.shell32.IsUserAnAdmin()" in body
    assert "current_user_sid() == storage._sid()" in body
