"""Opt-in native Shared Core test must not accept arbitrary candidate source paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_darwin_v0250_shared_smappservice_live_macos import (
    _optional_versioned_bridge_python,
    _optional_versioned_core_source,
    _require_single_versioned_source,
)


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    project = tmp_path / "project"
    root = project / "deployment/macos"
    root.mkdir(parents=True)
    selected = root / ".serverfs-v0250-candidate-0123456789ab"
    selected.mkdir(mode=0o700)
    (selected / "pyproject.toml").write_text(
        '[project]\nname = "serverfs-mcp"\nversion = "0.25.0"\n'
    )
    (selected / "uv.lock").write_text("locked\n")
    monkeypatch.setenv("SERVERFS_V0250_NATIVE_CORE_SOURCE", str(selected))
    return project, selected


def test_native_core_source_defaults_to_current_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVERFS_V0250_NATIVE_CORE_SOURCE", raising=False)
    assert _optional_versioned_core_source(tmp_path) == tmp_path


def test_versioned_native_core_accepts_only_expected_private_snapshot(
    candidate: tuple[Path, Path],
) -> None:
    project, selected = candidate
    assert _optional_versioned_core_source(project) == selected


@pytest.mark.parametrize(
    "bad",
    [
        "outside",
        "wrong-version",
        "symlink",
        "wrong-mode",
        "git-directory",
        "missing-lock",
    ],
)
def test_native_core_source_refuses_untrusted_candidate(
    candidate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    project, selected = candidate
    if bad == "outside":
        monkeypatch.setenv("SERVERFS_V0250_NATIVE_CORE_SOURCE", str(project))
    elif bad == "wrong-version":
        (selected / "pyproject.toml").write_text(
            '[project]\nname = "serverfs-mcp"\nversion = "0.24.2"\n'
        )
    elif bad == "symlink":
        link = selected.parent / ".serverfs-v0250-candidate-fedcba987654"
        link.symlink_to(selected, target_is_directory=True)
        monkeypatch.setenv("SERVERFS_V0250_NATIVE_CORE_SOURCE", str(link))
    elif bad == "wrong-mode":
        selected.chmod(0o755)
    elif bad == "git-directory":
        (selected / ".git").mkdir()
    elif bad == "missing-lock":
        (selected / "uv.lock").unlink()
    with pytest.raises(RuntimeError, match="native Core"):
        _optional_versioned_core_source(project)


@pytest.mark.parametrize("bridge_mode", ["same", "other", "checkout"])
def test_joint_native_candidate_rejects_mixed_source(
    candidate: tuple[Path, Path], bridge_mode: str
) -> None:
    project, selected = candidate
    same_bridge = selected / "bridge-venv/bin/python"
    if bridge_mode == "same":
        _require_single_versioned_source(project, selected, same_bridge)
    elif bridge_mode == "other":
        other_bridge = (
            selected.parent / ".serverfs-v0250-candidate-fedcba987654/bridge-venv/bin/python"
        )
        with pytest.raises(RuntimeError, match="must share one versioned source"):
            _require_single_versioned_source(project, selected, other_bridge)
    else:
        with pytest.raises(RuntimeError, match="must share one versioned source"):
            _require_single_versioned_source(project, project, same_bridge)


def test_joint_native_source_only_mode_keeps_existing_behavior(
    candidate: tuple[Path, Path],
) -> None:
    project, selected = candidate
    _require_single_versioned_source(project, selected, None)
    _require_single_versioned_source(project, project, None)


def test_snapshot_candidate_is_owned_by_current_user(candidate: tuple[Path, Path]) -> None:
    _, selected = candidate
    assert selected.stat().st_uid == os.getuid()


def test_versioned_native_bridge_defaults_to_source_owner(
    candidate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = candidate
    monkeypatch.delenv("SERVERFS_V0250_NATIVE_BRIDGE_PYTHON", raising=False)
    assert _optional_versioned_bridge_python(project) is None


@pytest.mark.parametrize("bad", ["outside", "missing", "wrong-mode", "bad-name"])
def test_versioned_native_bridge_rejects_unsafe_interpreter(
    candidate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    project, selected = candidate
    venv = selected / "bridge-venv"
    (venv / "bin").mkdir(parents=True)
    interpreter = venv / "bin/python"
    interpreter.write_text("synthetic interpreter")
    if bad == "outside":
        interpreter = project / "external-python"
    elif bad == "missing":
        interpreter.unlink()
    elif bad == "wrong-mode":
        selected.chmod(0o755)
    elif bad == "bad-name":
        interpreter = venv / "bin/python3"
    monkeypatch.setenv("SERVERFS_V0250_NATIVE_BRIDGE_PYTHON", str(interpreter))
    with pytest.raises(RuntimeError, match="native Bridge"):
        _optional_versioned_bridge_python(project)
