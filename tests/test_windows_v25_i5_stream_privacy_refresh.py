"""Fail-closed source contracts for the isolated I5 stream privacy upgrade."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-bridge-worker-stream-privacy-refresh.py"
)
SPEC = importlib.util.spec_from_file_location("v0250_i5_privacy_refresh", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


def test_stream_privacy_stage_has_unique_slot_and_no_task_or_network_side_effects() -> None:
    assert refresh.PREVIOUS_SLOT == "bridge-worker-partial-stream-refresh-1"
    assert refresh.NEW_SLOT == "bridge-worker-stream-privacy-refresh-1"
    assert refresh.ADAPTER == "adapters/claude.py"
    body = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "gate.BRIDGE",
        "named.IDS",
        "race._baseline(",
        "race._archive_matches(",
        "previous._check_wheel_matches_source(",
        "I5_PRIVACY_PRIVATE_WORKER_INSTALL=PASS",
        "I5_PRIVACY_INSTALLED_SOURCE_ACL_AND_SDK=PASS",
        '"--offline"',
        '"--no-deps"',
        '"--reinstall"',
        '"--dry-run"',
    ):
        assert marker in body
    for forbidden in (
        "git push",
        "Scheduler().start(",
        "Scheduler().stop(",
        "cloudflared tunnel",
        "httpx.get(",
        "subprocess.Popen(",
    ):
        assert forbidden not in body


def test_privacy_stage_checks_only_adapter_source_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    root = tmp_path / "agent_bridge" / "src" / "serverfs_agent_bridge"
    installed = tmp_path / "private-worker"
    for path in (root, installed):
        (path / "adapters").mkdir(parents=True)
        (path / "store.py").write_text("unchanged", encoding="utf-8")
    (installed / "adapters/claude.py").write_text("old raw telemetry", encoding="utf-8")
    (root / "adapters/claude.py").write_text(
        "include_partial_messages=True\n"
        "text_emission_recorded\n"
        '"agent.text_delta"\n'
        '"size_bytes": len(text.encode("utf-8"))\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh, "REPO", tmp_path)
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    prior = SimpleNamespace(CHECKS=("adapters/claude.py", "store.py"))
    refresh._source_delta_guard(prior, installed, "fixture-sid")

    (installed / "store.py").write_text("unrelated drift", encoding="utf-8")
    with pytest.raises(refresh.PrivacyRefreshError, match="unrelated"):
        refresh._source_delta_guard(prior, installed, "fixture-sid")


def test_privacy_stage_refuses_unsafe_raw_stream_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    root = tmp_path / "agent_bridge" / "src" / "serverfs_agent_bridge"
    installed = tmp_path / "private-worker"
    for path in (root, installed):
        (path / "adapters").mkdir(parents=True)
    (installed / "adapters/claude.py").write_text("old", encoding="utf-8")
    (root / "adapters/claude.py").write_text(
        "include_partial_messages=True\n"
        "text_emission_recorded\n"
        '"agent.text_delta"\n'
        '"size_bytes": len(text.encode("utf-8"))\n'
        'unsafe = {"text": text, "index": index}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh, "REPO", tmp_path)
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    with pytest.raises(refresh.PrivacyRefreshError, match="content-free"):
        refresh._source_delta_guard(
            SimpleNamespace(CHECKS=("adapters/claude.py",)),
            installed,
            "fixture-sid",
        )


def test_privacy_stage_wheel_is_unique_and_not_link(tmp_path: Path) -> None:
    with pytest.raises(refresh.PrivacyRefreshError):
        refresh._only_wheel(tmp_path / "missing")
    archive = tmp_path / "serverfs_agent_bridge-0.1-py3-none-any.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("serverfs_agent_bridge/store.py", "placeholder")
    assert refresh._only_wheel(tmp_path) == archive
    duplicate = tmp_path / "serverfs_agent_bridge-0.2-py3-none-any.whl"
    with zipfile.ZipFile(duplicate, "w") as wheel:
        wheel.writestr("serverfs_agent_bridge/store.py", "placeholder")
    with pytest.raises(refresh.PrivacyRefreshError, match="one exact"):
        refresh._only_wheel(tmp_path)
