"""Single-owner Windows Cloudflare service orchestration tests.

All scheduling, process and credential operations are mocked. No persistent
Task Scheduler registration is modified by these tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import cli
from serverfs_mcp import windows_service as service


def test_cloudflare_manifest_never_snapshots_secrets(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(service, "manifest_path", lambda: manifest)
    spec = SimpleNamespace(
        cloudflared=tmp_path / "cloudflared.exe",
        token_file=tmp_path / "token",
        port=18765,
        hostname="mcp.example.invalid",
        origin_environment={"SERVERFS_HTTP_API_KEY": "never-persist-this-value"},
    )
    service._write_manifest(
        config_path=tmp_path / "serverfs.toml",
        env_file=tmp_path / "origin.env",
        user_sid="S-1-5-21-1-2-3-4",
        task_executable=tmp_path / "pythonw.exe",
        task_xml=b"fixture task",
        cloudflare_spec=spec,
    )
    raw = manifest.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["provider"] == "cloudflare"
    assert document["cloudflare"]["hostname"] == "mcp.example.invalid"
    assert "never-persist-this-value" not in raw
    assert "origin_environment" not in raw


def test_legacy_manifest_remains_openai_by_default(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(service, "manifest_path", lambda: manifest)
    service._write_manifest(
        config_path=tmp_path / "serverfs.toml",
        env_file=tmp_path / "openai.env",
        user_sid="S-1-5-21-1-2-3-4",
        task_executable=tmp_path / "pythonw.exe",
        task_xml=b"fixture task",
    )
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert "provider" not in document
    assert "cloudflare" not in document


def test_provider_switch_refuses_active_windows_task(tmp_path: Path, monkeypatch):
    config = tmp_path / "serverfs.toml"
    env_file = tmp_path / "origin.env"
    config.write_text("fixture", encoding="utf-8")
    env_file.write_text("fixture", encoding="utf-8")
    spec = SimpleNamespace(config_path=config.resolve(), env_file=env_file.resolve())
    monkeypatch.setattr(service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(service, "_validate_persistent_runtime_location", lambda: None)
    monkeypatch.setattr(service, "_validate_runtime_backend", lambda: None)
    monkeypatch.setattr(service, "_read_env_file", lambda _: {})
    monkeypatch.setattr(service, "_current_user_sid", lambda: "S-1-5-21-1-2-3-4")
    monkeypatch.setattr(service, "_runner_executable", lambda: tmp_path / "pythonw.exe")
    monkeypatch.setattr(service, "_find_legacy_tunnel_roots", lambda: [])
    monkeypatch.setattr(service, "_query_task_xml", lambda **kwargs: "registered task")
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "provider": "openai",
            "task_valid": True,
            "enabled": True,
            "watchdog_alive": True,
            "runner_alive": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_run_schtasks",
        lambda *a, **k: pytest.fail("modified a running production task"),
    )
    with pytest.raises(service.WindowsServiceError, match="stop the existing provider"):
        service.install(config, env_file, cloudflare_spec=spec)


@pytest.mark.parametrize(
    "action",
    [
        "cloudflare_start",
        "cloudflare_stop",
        "cloudflare_restart",
        "cloudflare_uninstall",
    ],
)
def test_cloudflare_actions_never_touch_openai_provider(monkeypatch, action):
    monkeypatch.setattr(service, "_manifest_document", lambda: {"schema_version": 1})
    monkeypatch.setattr(service, "stop", lambda: pytest.fail("stopped OpenAI"))
    monkeypatch.setattr(service, "start", lambda: pytest.fail("started wrong provider"))
    monkeypatch.setattr(service, "uninstall", lambda: pytest.fail("uninstalled OpenAI"))
    with pytest.raises(service.WindowsServiceError, match="not installed"):
        getattr(service, action)()


def test_cloudflare_status_preserves_single_native_status(monkeypatch):
    monkeypatch.setattr(
        service, "_manifest_document", lambda: {"schema_version": 1, "provider": "cloudflare"}
    )
    monkeypatch.setattr(
        service, "status", lambda: {"provider": "cloudflare", "task_name": service.TASK_NAME}
    )
    assert service.cloudflare_status()["task_name"] == service.TASK_NAME


def test_cloudflare_manifest_refuses_unexpected_config_path(tmp_path: Path):
    document = {
        "provider": "cloudflare",
        "config_path": str(tmp_path / "serverfs.toml"),
        "env_file": str(tmp_path / "origin.env"),
        "cloudflare": {},
    }
    with pytest.raises(service.WindowsServiceError, match="path mismatch"):
        service._cloudflare_spec_from_manifest(
            document, tmp_path / "wrong.toml", tmp_path / "origin.env"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CLI route")
def test_windows_cloudflare_cli_routes_to_same_service(monkeypatch, capsys):
    monkeypatch.setattr(
        service,
        "cloudflare_status",
        lambda: {"provider": "cloudflare", "task_name": service.TASK_NAME},
    )
    args = argparse.Namespace(service_command="cloudflare", cloudflare_command="status")
    assert cli.cmd_service(args) == 0
    result = json.loads(capsys.readouterr().err)
    assert result["task_name"] == service.TASK_NAME


def test_cloudflare_child_liveness_requires_frozen_identity(monkeypatch):
    from serverfs_mcp import windows_process

    frozen = object()
    monkeypatch.setattr(
        service,
        "_managed_identity",
        lambda state, role: frozen if role == "connector" else None,
    )
    monkeypatch.setattr(windows_process, "same_process", lambda value: value is frozen)
    assert service._cloudflare_child_alive({}, "connector") is True
    assert service._cloudflare_child_alive({}, "origin") is False
