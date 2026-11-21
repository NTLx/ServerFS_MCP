"""Source-only I5 worker-wheel refresh contract; never installs/starts anything."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i5-bridge-worker-race-refresh.py"
)
_SPEC = importlib.util.spec_from_file_location("i5_race_refresh_contract", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
refresh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(refresh)


def test_i5_refresh_exact_scope_and_no_scheduler_lifecycle() -> None:
    assert refresh.INSTANCE == "i5-shared"
    assert "store.py" in refresh.CHECKS
    assert "service.py" in refresh.CHECKS
    assert len(refresh.CHECKS) == len(set(refresh.CHECKS))
    source = _SCRIPT.read_text(encoding="utf-8")
    for required in (
        "gate.protected_tasks()",
        "gate.verify_candidates(gate.candidates(), installed=True)",
        "named.fixture_tasks(installed=True)",
        "_current_identity(item) is None",
        '"--offline"',
        '"--no-deps"',
        '"--reinstall"',
        '"--dry-run"',
        "bridge-worker-race-refresh-1",
        "I5_WORKER_REFRESH_INSTALLED_POSTFLIGHT=PASS",
    ):
        assert required in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().restart(",
        ".install(manifest)",
        ".uninstall(manifest)",
        "cloudflared tunnel",
    ):
        assert forbidden not in source


def test_i5_refresh_wheel_byte_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / "repo"
    package = source_root / "agent_bridge/src/serverfs_agent_bridge"
    installed = tmp_path / "installed"
    for relative in refresh.CHECKS:
        (package / relative).parent.mkdir(parents=True, exist_ok=True)
        (installed / relative).parent.mkdir(parents=True, exist_ok=True)
        data = ("checked:" + relative).encode()
        (package / relative).write_bytes(data)
        (installed / relative).write_bytes(data)
    wheel = tmp_path / "serverfs_agent_bridge-0.25.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative in refresh.CHECKS:
            archive.write(package / relative, "serverfs_agent_bridge/" + relative)
    monkeypatch.setattr(refresh, "REPO", source_root)
    assert refresh._archive_matches(wheel, None)
    assert refresh._archive_matches(wheel, installed)
    (installed / "store.py").write_bytes(b"foreign-drift")
    assert not refresh._archive_matches(wheel, installed)
    (package / "store.py").write_bytes(b"unbuilt-source-change")
    assert not refresh._archive_matches(wheel, None)


def test_i5_refresh_guard_refuses_bad_package_state() -> None:
    with pytest.raises(refresh.I5WorkerRefreshError):
        refresh.require(False, "candidate wheel mismatch")
    refresh.require(True, "accepted")
