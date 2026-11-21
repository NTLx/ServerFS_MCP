"""Source-only safety contracts for I5 one-use partial-stream Worker upgrade."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-bridge-worker-partial-stream-refresh.py"
)
_SPEC = importlib.util.spec_from_file_location("i5_partial_worker_refresh_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
refresh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(refresh)


def test_i5_partial_upgrade_is_independent_one_use_and_never_starts_services() -> None:
    assert refresh.NEW_SLOT == "bridge-worker-partial-stream-refresh-1"
    assert refresh.PREVIOUS_SLOT == "bridge-worker-race-refresh-1"
    assert refresh.CHANGED == "adapters/claude.py"
    source = _SCRIPT.read_text(encoding="utf-8")
    for required in (
        "gate.BRIDGE",
        "named.IDS",
        "previous._baseline(",
        "previous._archive_matches(",
        "include_partial_messages=True",
        "I5_STREAM_REFRESH_CLAUDE_ONLY_SOURCE_DIFF=PASS",
        "I5_STREAM_REFRESH_INSTALLED_EXACT_AND_PRIVATE=PASS",
        '"--offline"',
        '"--no-deps"',
        '"--reinstall"',
        '"--dry-run"',
    ):
        assert required in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().install(",
        "cloudflared tunnel",
        "Get-Content",
        "git push",
        "subprocess.Popen(",
    ):
        assert forbidden not in source


def test_i5_partial_change_exactly_one_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    source = tmp_path / "repo/agent_bridge/src/serverfs_agent_bridge"
    package = tmp_path / "installed"
    checked = ("adapters/claude.py", "store.py")
    adapter = (
        "include_partial_messages=True\n"
        '"agent.text_delta"\n'
        "message.parent_tool_use_id is not None\n"
        '"text_delta"\n'
    )
    for path in (source, package):
        (path / "adapters").mkdir(parents=True)
    (source / "adapters/claude.py").write_text(adapter, encoding="utf-8")
    (package / "adapters/claude.py").write_text("old adapter", encoding="utf-8")
    (source / "store.py").write_text("same", encoding="utf-8")
    (package / "store.py").write_text("same", encoding="utf-8")
    monkeypatch.setattr(refresh, "REPO", tmp_path / "repo")
    monkeypatch.setattr(storage, "check_private", lambda *args, **kwargs: None)
    original = SimpleNamespace(CHECKS=checked)
    refresh._source_change_is_exact(original, package, "test-sid")

    (package / "store.py").write_text("drift", encoding="utf-8")
    with pytest.raises(refresh.I5StreamRefreshError, match="unrelated"):
        refresh._source_change_is_exact(original, package, "test-sid")


def test_i5_partial_source_wheel_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "repo/agent_bridge/src/serverfs_agent_bridge"
    (source / "adapters").mkdir(parents=True)
    (source / "adapters/claude.py").write_text("new", encoding="utf-8")
    (source / "store.py").write_text("unchanged", encoding="utf-8")
    wheel = tmp_path / "serverfs_agent_bridge-0.24.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative in ("adapters/claude.py", "store.py"):
            archive.write(source / relative, "serverfs_agent_bridge/" + relative)
    monkeypatch.setattr(refresh, "REPO", tmp_path / "repo")
    original = SimpleNamespace(CHECKS=("adapters/claude.py", "store.py"))
    refresh._check_wheel_matches_source(wheel, original)
    (source / "adapters/claude.py").write_text("drift", encoding="utf-8")
    with pytest.raises(refresh.I5StreamRefreshError, match="parity"):
        refresh._check_wheel_matches_source(wheel, original)
