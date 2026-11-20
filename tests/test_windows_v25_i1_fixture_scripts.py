"""Windows I1 native staging/smoke operator scripts: pure synthetic contracts.

These tests import scripts as modules but NEVER run their __main__, Windows
Scheduler, uv, network, or production credential code.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "deployment" / "windows"


def _script(name: str):
    path = _ROOT / name
    spec = importlib.util.spec_from_file_location("i1_fixture_" + name.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stager_refuses_untrusted_interpreter_and_production_reuse() -> None:
    stage = _script("v0250-i1-stage.py")
    assert stage.INSTANCE == "i1-smoke"
    assert stage.PORT == 18868
    assert stage.NATIVE_WHEEL_SHA256 == (
        "127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3"
    )
    source = (_ROOT / "v0250-i1-stage.py").read_text(encoding="utf-8")
    assert 'storage.prepare("core", INSTANCE)' in source
    assert '"--offline"' in source
    assert '"--no-build-isolation"' not in source
    assert '"--no-index"' not in source
    assert '"--dry-run"' in source
    assert 'private_env["UV_LINK_MODE"] = "copy"' in source
    assert 'storage.home("core", INSTANCE)' in source
    assert "SERVERFS_HTTP_API_KEY=" in source
    assert "secrets.token_urlsafe(48)" in source
    assert "storage.pinned_pythonw(" not in source  # No prod venv copy.
    assert "Remove-Item" not in source


def test_stager_runs_only_fixed_offline_process_args(monkeypatch) -> None:
    stage = _script("v0250-i1-stage.py")
    seen = []

    def fake_run(argv, *, cwd, env, stdin, capture_output, timeout, check):
        seen.append(argv)
        assert stdin is subprocess.DEVNULL
        assert capture_output is True
        assert timeout == 150
        assert not check
        return subprocess.CompletedProcess(argv, 0, b"PASSED", b"")

    monkeypatch.setattr(stage.subprocess, "run", fake_run)
    assert stage.run(["uv", "build", "--offline"]) == "PASSED"
    assert seen == [["uv", "build", "--offline"]]


def test_stager_refuses_failed_package_build_without_echoing_stderr(monkeypatch) -> None:
    stage = _script("v0250-i1-stage.py")
    monkeypatch.setattr(
        stage.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 23, b"", b"SECRET!!"),
    )
    with pytest.raises(stage.StageRefusal, match="isolated step failed") as caught:
        stage.run(["uv", "build", "--offline"])
    assert "SECRET!!" not in str(caught.value)


def test_stager_private_file_is_create_only(tmp_path: Path, monkeypatch) -> None:
    stage = _script("v0250-i1-stage.py")
    from serverfs_mcp import windows_private_artifacts
    from serverfs_mcp.windows_v25 import storage

    protected = []
    monkeypatch.setattr(
        windows_private_artifacts,
        "protect_new_file",
        lambda path, sid: protected.append(path),
    )
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    target = tmp_path / "http.env"
    stage.private_file(target, b"token=redacted", "S-1-5-21-111-222-333-1001")
    assert target.read_bytes() == b"token=redacted"
    assert protected == [target]
    with pytest.raises(stage.StageRefusal, match="already occupied"):
        stage.private_file(target, b"overwrite", "S-1-5-21-111-222-333-1001")
    assert target.read_bytes() == b"token=redacted"


def test_smoke_only_operates_approved_named_core_and_has_final_stop() -> None:
    smoke = _script("v0250-i1-live-smoke.py")
    assert smoke.INSTANCE == "i1-smoke"
    assert smoke.PORT == 18868
    assert smoke.TASK == "ServerFS v25 i1-smoke Core"
    script = (_ROOT / "v0250-i1-live-smoke.py").read_text(encoding="utf-8")
    assert 'cli("stop")' in script
    assert 'cli("start")' in script
    assert re.search(r'"--instance",\s*INSTANCE', script) is not None
    assert "Get-ScheduledTask" in script
    assert "asyncio.wait_for(exercise_mcp(key), timeout=60)" in script
    assert 'mark("fixture-start")' in script
    assert 'mark("fixture-stop")' in script
    assert 'mark("mcp-create-file")' in script
    assert 'mark("mcp-delete-file")' in script
    assert "I1_FIXTURE_STOP=NOT_VERIFIED" in script
    assert "I1_NATIVE_SMOKE=FAILED phase=" in script
    assert "secrets.token_hex(12)" in script
    assert "I1_CLI_" in script
    assert "I1_HTTP_ORIGIN_STATUS=" in script
    assert "I1_HTTP_ORIGIN=REQUEST_FAILED type=" in script
    assert "file-only Core exposed an Agent or repository execution tool" in script
    assert 'fixture.get("agent_mode") == "disabled"' in script
    assert "Stop-Process" not in script
    assert "Stop-ScheduledTask" not in script
    assert "Disable-ScheduledTask" not in script


@pytest.mark.parametrize("action", ["uninstall", "restart", "/End", "disable"])
def test_smoke_refuses_non_allowlisted_lifecycle_verbs(action: str) -> None:
    smoke = _script("v0250-i1-live-smoke.py")
    with pytest.raises(smoke.SmokeError, match="not allowlisted"):
        smoke.cli(action)


def test_smoke_lifecycle_errors_are_classified_without_raw_credentials(monkeypatch, capsys) -> None:
    smoke = _script("v0250-i1-live-smoke.py")
    raw = b"SECRET-CREDENTIAL component start refused at ready; rollback=disabled-and-ended"
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, b"", raw),
    )
    with pytest.raises(smoke.SmokeError, match="isolated Core lifecycle operation refused"):
        smoke.cli("start")
    output = capsys.readouterr().err
    assert "I1_CLI_START=FAILED category=start-readiness" in output
    assert "SECRET-CREDENTIAL" not in output


def test_smoke_rejects_changed_production_task_state(monkeypatch) -> None:
    smoke = _script("v0250-i1-live-smoke.py")
    rows = [
        {"name": name, "state": state, "enabled": on}
        for name, state, on in (
            ("ServerFS Native Tunnel", "Running", True),
            ("ServerFS v25 Core", "Running", True),
            ("ServerFS v25 Cloudflare Connector", "Disabled", False),
            ("ServerFS v25 Agent Bridge", "Disabled", False),
            ("ServerFS v25 OpenAI Connector", "Disabled", False),
        )
    ]
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows).encode("utf-8"), b""
        ),
    )
    with pytest.raises(smoke.SmokeError, match="production task baseline"):
        smoke.production_snapshot()


def test_smoke_extracts_file_revision_without_exposing_payload() -> None:
    smoke = _script("v0250-i1-live-smoke.py")
    record = SimpleNamespace(
        isError=False,
        structuredContent={"result": {"revision": "v1:abcdef"}},
        content=[],
    )
    assert smoke.result_mapping(record) == {"revision": "v1:abcdef"}
    with pytest.raises(smoke.SmokeError, match="rejected"):
        smoke.result_mapping(SimpleNamespace(isError=True, structuredContent={}, content=[]))


def test_existing_preflight_matches_stager_identity_and_port() -> None:
    stage = _script("v0250-i1-stage.py")
    preflight = (_ROOT / "v0250-i1-native-preflight.ps1").read_text(encoding="utf-8")
    assert "[string]$InstanceId = 'i1-smoke'" in preflight
    assert "[int]$Port = 18868" in preflight
    assert stage.INSTANCE == "i1-smoke"
    assert stage.PORT == 18868
