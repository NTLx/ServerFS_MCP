"""I7 create-only nonproduction Core and verified private cloudflared binary contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-nonprod-core-stage.py"
_SPEC = importlib.util.spec_from_file_location("i7_nonprod_core_stage", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
stage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(stage)


def test_i7_staging_reserves_new_core_without_connectors_or_tokens() -> None:
    assert stage.INSTANCE == "i7-public"
    assert stage.PORT == 18875
    assert stage.HOST == "i7-public.local"
    assert stage.CONNECTORS == ("i7-edge-a", "i7-edge-b")
    assert (
        stage.CLOUDFLARED_SHA256
        == "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"
    )
    source = _SCRIPT.read_text(encoding="utf-8")
    for requirement in (
        "v0250-i5-bridge-postfail-readonly.py",
        "v0250-i6-named-connectors-postflight-readonly.py",
        "native_pin.verify_native_wheel",
        "installer._wheel",
        "i3._installed_core",
        "storage.prepare",
        "storage.stage",
        "Scheduler().install",
        "CLOUDFLARED_SHA256",
        "I7_CONNECTOR_TOKENS_AND_TASKS=NOT_CREATED",
    ):
        assert requirement in source
    for forbidden in (
        "Scheduler().start",
        "token_file",
        "cloudflare.request(",
        "cloudflared tunnel",
        "git push",
        "uv pip install --index",
        "input(",
    ):
        assert forbidden not in source


def test_i7_only_readonly_fixture_and_agent_disabled(tmp_path: Path) -> None:
    fixture = tmp_path / "private_fixture"
    fixture.mkdir()
    doc = tomllib.loads(stage._policy(fixture).decode("utf-8"))
    assert doc["agent"]["enabled"] is False
    assert len(doc["workdirs"]) == 1
    entry = doc["workdirs"][0]
    assert entry["alias"] == "Fixture"
    assert entry["path"] == str(fixture)
    assert entry["read_only"] is True
    assert entry["agent_mode"] == "disabled"
    assert entry["repo_ops_enabled"] is False
    assert entry["agent_runtimes"] == []


def test_i7_generated_toml_passes_real_native_policy_parser(tmp_path: Path) -> None:
    from serverfs_mcp.native_config import load_native_config

    fixture = tmp_path / "read_only"
    fixture.mkdir()
    config = tmp_path / "serverfs.toml"
    config.write_bytes(stage._policy(fixture))
    workdirs, policy = load_native_config(config)
    assert policy.bridge_required is False
    assert len(workdirs) == 1
    assert workdirs[0].alias == "Fixture"
    assert workdirs[0].read_only is True
    assert workdirs[0].root == fixture


def test_i7_cloudflared_copy_never_links_to_live_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_private_artifacts
    from serverfs_mcp.windows_v25 import storage

    original = tmp_path / "production.exe"
    destination = tmp_path / "staging.exe"
    original.write_bytes(b"verified-vendor-build-for-test")
    monkeypatch.setattr(
        stage,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(original.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args, **kwargs: None)
    stage._copy_private_binary(original, destination, "fixture-sid")
    assert destination.read_bytes() == original.read_bytes()
    assert destination.stat().st_ino != original.stat().st_ino
    with pytest.raises(stage.I7Refusal, match="occupied"):
        stage._copy_private_binary(original, destination, "fixture-sid")
    assert original.read_bytes() == b"verified-vendor-build-for-test"


def test_i7_copied_binary_sha_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import windows_private_artifacts
    from serverfs_mcp.windows_v25 import storage

    source = tmp_path / "origin.exe"
    dest = tmp_path / "candidate.exe"
    source.write_bytes(b"bad-copy")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    monkeypatch.setattr(windows_private_artifacts, "protect_new_file", lambda *args, **kwargs: None)
    with pytest.raises(stage.I7Refusal, match="copy differs"):
        stage._copy_private_binary(source, dest, "fixture-sid")
    assert source.read_bytes() == b"bad-copy"


def test_i7_postflight_requires_no_connector_tokens_or_runtime() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert 'not (base / "services/cloudflare/connectors").exists()' in source
    assert 'not (base / "envs/cloudflare/Scripts/python.exe").exists()' in source
    assert "I7_ONE_DISABLED_CORE_NO_CONNECTORS=PASS" in source
    assert "I7_POSTFLIGHT=PASS" in source
