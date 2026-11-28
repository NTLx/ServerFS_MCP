"""Deterministic no-production tests for the reviewed macOS candidate source gate."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[1] / "deployment/macos/v0250_candidate_source_gate.py"
_SPEC = importlib.util.spec_from_file_location("v0250_candidate_source_gate", _GATE)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=10)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    root = (tmp_path / "repo").resolve()
    root.mkdir(mode=0o700)
    _git(root, "init", "-q")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.25.0'\n")
    (root / "uv.lock").write_text("synthetic-lock\n")
    (root / "src").mkdir()
    (root / "src/app.py").write_text("VALUE = 'committed'\n")
    _git(root, "add", "pyproject.toml", "uv.lock", "src/app.py")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    (root / "src/app.py").write_text("VALUE = 'reviewed dirty source'\n")
    (root / "tests").mkdir()
    (root / "tests/test_feature.py").write_text("def test_synthetic(): pass\n")
    manifest = tmp_path / "reviewed.txt"
    manifest.write_text("src/app.py\ntests/test_feature.py\n", encoding="utf-8")
    return root, manifest


def test_check_exact_reviewed_dirty_paths(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    entries, scratch, sha = gate.verify_candidate(root, manifest)
    assert entries == ("src/app.py", "tests/test_feature.py")
    assert scratch == 0
    assert len(sha) == 40


def test_unreviewed_new_file_fails_closed(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    (root / "src/unreviewed.py").write_text("UNREVIEWED = True\n")
    with pytest.raises(gate.CandidateSourceError, match="unreviewed"):
        gate.verify_candidate(root, manifest)


def test_preexisting_reviewed_path_not_dirty_fails_closed(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    _git(root, "checkout", "--", "src/app.py")
    with pytest.raises(gate.CandidateSourceError, match="stale"):
        gate.verify_candidate(root, manifest)


def test_staged_changes_fail_closed(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    _git(root, "add", "src/app.py")
    with pytest.raises(gate.CandidateSourceError, match="staged"):
        gate.verify_candidate(root, manifest)


@pytest.mark.parametrize(
    "bad", ["../outside.py", "/tmp/bad.py", "src/../app.py", ".env", "src/.hidden.py"]
)
def test_unsafe_manifest_paths_fail(repo: tuple[Path, Path], bad: str) -> None:
    _, manifest = repo
    manifest.write_text(f"{bad}\n")
    with pytest.raises(gate.CandidateSourceError, match="unsafe"):
        gate.read_manifest(manifest)


@pytest.mark.parametrize(
    "metadata",
    [
        "pyproject.toml",
        "uv.lock",
        "plugin.json",
        "agent_bridge/pyproject.toml",
        "agent_bridge/uv.lock",
    ],
)
def test_explicit_release_metadata_is_permitted(metadata: str) -> None:
    gate._safe_path(metadata)


@pytest.mark.parametrize(
    "unreviewed",
    ["serverfs.toml", "secrets.json", "agent_bridge/private.lock", "deployment/config.json"],
)
def test_unlisted_metadata_names_are_still_rejected(unreviewed: str) -> None:
    with pytest.raises(gate.CandidateSourceError, match="unsafe"):
        gate._safe_path(unreviewed)


def test_release_workflow_exact_path_is_permitted() -> None:
    gate._safe_path(".github/workflows/wheel-release.yml")


@pytest.mark.parametrize(
    "unreviewed",
    [".github/workflows/container.yml", ".github/other.yml", "deployment/release.yml"],
)
def test_workflow_allowlist_does_not_generalize(unreviewed: str) -> None:
    with pytest.raises(gate.CandidateSourceError, match="unsafe"):
        gate._safe_path(unreviewed)


def test_generated_scratch_is_excluded_without_becoming_release_input(
    repo: tuple[Path, Path],
) -> None:
    root, manifest = repo
    scratch = root / "deployment/macos/.serverfs-v0250-wheel-fixture"
    scratch.mkdir(parents=True)
    (scratch / "wheel.whl").write_bytes(b"synthetic cache")
    entries, count, _ = gate.verify_candidate(root, manifest)
    assert count == 1
    assert "deployment/macos/.serverfs-v0250-wheel-fixture/wheel.whl" not in entries


def test_candidate_symlink_is_rejected(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    (root / "tests/test_feature.py").unlink()
    os.symlink("missing.py", root / "tests/test_feature.py")
    with pytest.raises(gate.CandidateSourceError, match="disappeared"):
        gate.verify_candidate(root, manifest)


def test_snapshot_overlays_only_exact_reviewed_sources(
    repo: tuple[Path, Path], tmp_path: Path
) -> None:
    root, manifest = repo
    (root / "deployment/macos/.serverfs-v0250-synthetic").mkdir(parents=True)
    (root / "deployment/macos/.serverfs-v0250-synthetic/cache.whl").write_bytes(b"skip")
    output = tmp_path.resolve() / "isolated-snapshot"
    count, scratch, sha = gate.snapshot_candidate(root, manifest, output)
    assert count == 2
    assert scratch == 1
    assert len(sha) == 40
    assert (output / "src/app.py").read_text() == "VALUE = 'reviewed dirty source'\n"
    assert (output / "tests/test_feature.py").read_text() == "def test_synthetic(): pass\n"
    assert (output / "uv.lock").read_text() == "synthetic-lock\n"
    assert not (output / ".git").exists()
    assert not (output / "deployment").exists()
    assert (root / "src/app.py").read_text() == "VALUE = 'reviewed dirty source'\n"
    with pytest.raises(gate.CandidateSourceError, match="new outside path"):
        gate.snapshot_candidate(root, manifest, output)


def test_snapshot_allows_only_randomized_private_repo_scratch(repo: tuple[Path, Path]) -> None:
    root, manifest = repo
    scratch_parent = root / "deployment/macos"
    scratch_parent.mkdir(parents=True)
    target = scratch_parent / ".serverfs-v0250-candidate-0123456789ab"
    count, _, sha = gate.snapshot_candidate(root, manifest, target)
    assert count == 2 and len(sha) == 40
    assert (target / "src/app.py").read_text() == "VALUE = 'reviewed dirty source'\n"
    unreviewed = scratch_parent / "unexpected-output"
    with pytest.raises(gate.CandidateSourceError, match="new outside path"):
        gate.snapshot_candidate(root, manifest, unreviewed)
