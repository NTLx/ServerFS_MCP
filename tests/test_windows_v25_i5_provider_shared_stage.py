"""Source-only I5 real-Claude-ready shared Bridge create-only stage contracts.

No installation, secret provisioning, provider calls, Scheduler or network.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

from serverfs_mcp.instance_identity import InstanceIdentity

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-provider-shared-stage.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_provider_stage_source", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i5_shared_bridge_issues_only_two_core_ids_and_signed_task_methods() -> None:
    module = _module()
    assert module.BRIDGE == "i5-shared"
    assert module.CORE_PORTS == {"i5-core-a": 18873, "i5-core-b": 18874}
    assert module.METHODS == (
        "bridge.ping",
        "runtime.list",
        "task.submit",
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.approval.respond",
        "task.question.answer",
    )
    for core_id in module.CORE_PORTS:
        assert InstanceIdentity(core_id, "shared", module.BRIDGE).bridge_id == module.BRIDGE


def test_i5_bridge_config_only_allows_claude_in_new_fixture(tmp_path: Path) -> None:
    module = _module()
    home = tmp_path / "bridge"
    fixture = tmp_path / "fixture"
    data = module._bridge_document(
        home=home,
        fixture=fixture,
        sid="S-1-5-21-100-200-300-1001",
        cli=str(tmp_path / "claude.exe"),
    )
    assert data["bridge_id"] == "i5-shared"
    assert data["core_grants_enabled"] is True
    assert data["enable_fake_runtime"] is False
    assert data["claude"] == {
        "enabled": True,
        "claude_bin": str(tmp_path / "claude.exe"),
        "use_proxy": False,
    }
    assert len(data["workdirs"]) == 1
    policy = data["workdirs"][0]
    assert policy["alias"] == "Fixture"
    assert policy["host_path"] == str(fixture)
    assert policy["read_only"] is False
    assert policy["agent_mode"] == "workspace-write"
    assert policy["agent_runtimes"] == ["claude"]
    assert policy["repo_ops_enabled"] is False
    assert "secret_hex" not in json.dumps(data)
    assert "http_api_key" not in json.dumps(data)


def test_i5_core_policy_matches_provider_enabled_writable_scope(tmp_path: Path) -> None:
    from serverfs_mcp.native_config import load_native_config

    source = tmp_path / "serverfs.toml"
    source.write_bytes(_module()._core_toml(tmp_path / "fixture"))
    policy = tomllib.loads(source.read_text(encoding="utf-8"))
    assert policy["agent"]["enabled"] is True
    assert policy["agent"]["claude"]["enabled"] is True
    workdirs, settings = load_native_config(source)
    assert settings.bridge_required
    assert settings.agent is not None and settings.agent.claude.enabled
    assert workdirs[0].agent_runtimes == frozenset({"claude"})
    assert workdirs[0].read_only is False
    assert len(policy["workdirs"]) == 1
    directory = policy["workdirs"][0]
    assert directory["alias"] == "Fixture"
    assert directory["read_only"] is False
    assert directory["agent_mode"] == "workspace-write"
    assert directory["agent_runtimes"] == ["claude"]
    assert directory["repo_ops_enabled"] is False


def test_i5_stage_uses_oneuse_private_pinned_install_and_no_starts() -> None:
    source = _SOURCE.read_text("utf-8")
    for needed in (
        "gate.preflight()",
        "native_pin.verify_native_wheel(native)",
        'storage.prepare("bridge", BRIDGE)',
        'storage.prepare("core", core_id)',
        "installer._new_private_file(",
        "installer._protect_tree(",
        "issue_shared_core_capabilities(",
        "verify_core_bridge_workdirs(",
        "load_native_core_bridge_binding(",
        "load_native_bridge_plan(",
        "Scheduler().install(manifest)",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "I5_NATIVE_STAGE=PASS",
    ):
        assert needed in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "Start-ScheduledTask",
        "Stop-ScheduledTask",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "shutil.rmtree(",
        "os.system(",
        "git push",
        "git commit",
        "claude_agent_sdk",
        "model_call(",
    ):
        assert forbidden not in source
