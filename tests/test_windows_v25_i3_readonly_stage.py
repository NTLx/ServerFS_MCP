"""I3 new protected read-only shared Bridge + two Core create-only stage contracts.

Tests intentionally never call main or create a Windows Scheduler service.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.native_config import load_native_config
from serverfs_mcp.windows_v25.bridge_resources import WindowsBridgeResources
from serverfs_mcp.windows_v25.core_bridge_binding import verify_core_bridge_workdirs

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment" / "windows" / "v0250-i3-readonly-stage.py"
)
_SID = "S-1-5-21-111-222-333-1001"


def _module():
    spec = importlib.util.spec_from_file_location("v0250_i3_stage_source", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i3_three_distinct_new_instances_with_no_fake_or_runtime_execution() -> None:
    stage = _module()
    assert stage.BRIDGE == "i3-shared"
    assert stage.CORE_PORTS == {"i3-core-a": 18869, "i3-core-b": 18870}
    assert stage._METHODS == ("bridge.ping", "runtime.list")
    assert "task.submit" not in stage._METHODS
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "gate.main()" in source
    assert 'storage.prepare("bridge", BRIDGE)' in source
    assert 'storage.prepare("core", core_id)' in source
    assert "issue_shared_core_capabilities(" in source
    assert "methods=_METHODS" in source
    assert '"enable_fake_runtime": False' in source
    assert "gate.verify_task_inventory(production)" in source
    assert source.index("I3_FOUR_PRIVATE_VENVS=VERIFIED") < source.index(
        "I3_TWO_DISTINCT_HMAC_GRANTS=PRIVATE"
    )
    assert source.index("I3_TWO_DISTINCT_HMAC_GRANTS=PRIVATE") < source.index(
        "storage.stage(manifest)"
    )
    assert source.index("storage.stage(manifest)") < source.index("Scheduler().install(manifest)")
    assert "I3_PRODUCTION_TASKS=UNCHANGED" in source
    assert "I3_NATIVE_STAGE=PASS" in source


def test_readonly_core_toml_and_bridge_config_must_agree(tmp_path: Path) -> None:
    stage = _module()
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    owner_home = tmp_path / "i3-shared" / "services" / "bridge"
    owner_home.mkdir(parents=True)
    policy = stage._bridge_policy(sid=_SID, home=owner_home, fixture=shared_root)
    assert policy["bridge_id"] == "i3-shared"
    assert policy["core_grants_enabled"] is True
    assert policy["socket_path"] == WindowsBridgeResources(_SID, stage.BRIDGE).pipe_name
    assert policy["workdirs"][0]["read_only"] is True
    assert policy["workdirs"][0]["agent_runtimes"] == []
    assert policy["workdirs"][0]["repo_ops_enabled"] is False
    assert "secret_hex" not in stage._json(policy).decode("utf-8")
    root = tmp_path / "core.toml"
    root.write_bytes(stage._core_policy(shared_root))
    workdirs, settings = load_native_config(root)
    assert settings.bridge_required
    assert len(workdirs) == 1
    verify_core_bridge_workdirs(
        workdirs, policy, canonicalize=lambda raw: Path(raw).resolve(strict=True)
    )


def test_i3_stage_rejects_changed_installation_on_mocked_offline_core(
    tmp_path, monkeypatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    stage = _module()
    root = tmp_path / "envs" / "core"
    installed = root / "Lib" / "site-packages" / "serverfs_mcp" / "windows_v25"
    installed.mkdir(parents=True)
    (installed / "runtime.py").write_bytes(b"bad-payload")
    original = stage.REPO
    monkeypatch.setattr(stage, "REPO", tmp_path / "checkout")
    source = stage.REPO / "src" / "serverfs_mcp" / "windows_v25" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"correct-payload")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)

    def fake_install(venv, wheels):
        return root / "Scripts" / "python.exe", root / "Scripts" / "pythonw.exe"

    fake = SimpleNamespace(
        _offline_install=fake_install,
        _protect_tree=lambda *args, **kwargs: None,
        run=lambda *args, **kwargs: b"",
    )
    with pytest.raises(stage.I3StageError, match="differs"):
        stage._installed_core(fake, sid=_SID, venv=root, wheels=[])
    monkeypatch.setattr(stage, "REPO", original)


def test_i3_stage_source_never_operates_incumbent_tasks_or_logs_secrets() -> None:
    source = _SCRIPT.read_text("utf-8")
    assert "Scheduler().uninstall(" not in source
    assert "Scheduler().start(" not in source
    assert "Scheduler().stop(" not in source
    assert "Stop-Process" not in source
    assert "Remove-Item" not in source
    assert "git push" not in source
    assert "print(private_grants)" not in source
    assert "print(secret)" not in source
    assert "print(payload)" not in source
    assert "I3_NATIVE_STAGE=FAILED phase=" in source
