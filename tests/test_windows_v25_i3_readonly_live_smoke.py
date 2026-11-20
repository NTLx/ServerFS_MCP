"""I3 read-only multi-Core/one-Bridge native lifecycle smoke source contracts.

Never executes real Task Scheduler or an HTTP request.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i3-readonly-live-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i3_live_readonly_smoke", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_bridge_and_two_core_stop_after_start_and_signed_scope() -> None:
    smoke = _module()
    assert smoke.TASKS == (
        ("bridge", "i3-shared"),
        ("core", "i3-core-a"),
        ("core", "i3-core-b"),
    )
    assert smoke.CORE_PORTS == {"i3-core-a": 18869, "i3-core-b": 18870}
    src = _SCRIPT.read_text("utf-8")
    assert "load_native_core_bridge_binding(manifest, workdirs)" in src
    assert "make_bridge_peer_validator(bridge_id=BRIDGE, user_sid=sid)" in src
    assert 'client.call("bridge.ping", {"workdir": "Fixture"})' in src
    assert 'client.call("runtime.list", {})' in src
    assert "I3_CROSS_CORE_HMAC_SPOOF=DENIED" in src
    assert '"CORE_NOT_AUTHORIZED"' in src
    assert "streamable_http_client(url, http_client=http)" in src
    assert "trust_env=False" in src
    assert "list_agent_runtimes" in src
    assert '"create_text_file"' in src
    assert "I3_FIXTURE_STOP=" in src
    assert "I3_PRODUCTION_TASKS_AND_CORE=UNCHANGED" in src
    assert "I3_NATIVE_SHARED_BRIDGE=PASS" in src
    assert "reversed(started)" in src


@pytest.mark.parametrize(
    "component, instance, action",
    [
        ("bridge", "i2-smoke", "start"),
        ("core", "default", "stop"),
        ("cloudflare", "i3-core-a", "start"),
        ("bridge", "i3-shared", "uninstall"),
        ("core", "i3-core-a", "restart"),
    ],
)
def test_scoped_cli_rejects_foreign_lifecycle_commands(component, instance, action) -> None:
    smoke = _module()
    with pytest.raises(smoke.I3SmokeError):
        smoke.cli(component, instance, action)


def test_scoped_cli_suppresses_raw_stderr(monkeypatch, capsys) -> None:
    smoke = _module()
    from serverfs_mcp.windows_v25 import storage

    monkeypatch.setattr(storage, "root", lambda instance: Path("/mock/instance"))
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "_sid", lambda: "fake")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, b"", b"SECRET-KEY"),
    )
    with pytest.raises(smoke.I3SmokeError):
        smoke.cli("core", "i3-core-a", "start")
    assert "SECRET-KEY" not in capsys.readouterr().err


def test_tool_mapping_accepts_sdk_snake_case_success_and_denies_error() -> None:
    smoke = _module()
    result = SimpleNamespace(
        is_error=False, structured_content={"result": {"workdirs": []}}, content=[]
    )
    assert smoke._mapping(result) == {"workdirs": []}
    denied = SimpleNamespace(is_error=True, structured_content={}, content=[])
    with pytest.raises(smoke.I3SmokeError):
        smoke._mapping(denied)


def test_owner_stop_is_scoped_and_no_install_or_provider_invocation() -> None:
    src = _SCRIPT.read_text("utf-8")
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "storage.stage(",
        "Stop-Process",
        "Remove-Item",
        "git push",
        "task.submit",
        "run_repo_action",
    ):
        assert forbidden not in src
    assert 'cli(component, instance, "stop")' in src
    assert "three_task_states(preflight)" in src
    assert "production_snapshot(preflight) == baseline" in src
