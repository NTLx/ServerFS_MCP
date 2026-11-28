"""Exact Git SHA offline runtime build input: never release a mutable worktree.

All repositories are private local pytest fixtures. No remotes, native services,
production paths, or network are involved. Real Git archive is exercised.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from serverfs_mcp import darwin_shared_release_source as release
from serverfs_mcp import darwin_shared_runtime as runtime


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def committed(tmp_path: Path) -> dict[str, object]:
    root = tmp_path.resolve() / "checkout"
    root.mkdir(mode=0o700)
    _git(root, "init", "-q")
    (root / "pyproject.toml").write_text(
        "[project]\nname='synthetic'\nversion='0.25.0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("committed-lock\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "entry.py").write_text("VERSION = 'committed'\n")
    _git(root, "add", "pyproject.toml", "uv.lock", "src/entry.py")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "synthetic release input",
    )
    sha = _git(root, "rev-parse", "HEAD")
    return {"root": root, "sha": sha}


def test_release_archive_comes_from_exact_commit_not_dirty_checkout(
    committed: dict[str, object], tmp_path: Path
) -> None:
    root = committed["root"]
    sha = committed["sha"]
    assert isinstance(root, Path) and isinstance(sha, str)
    (root / "src" / "entry.py").write_text("VERSION = 'uncommitted'\n")
    (root / "untracked-secret.env").write_text("SHOULD_NOT_BE_ARCHIVED\n")
    output = tmp_path.resolve() / "release-snapshot"
    assert release.materialize_release_source(root, sha, output) == output
    assert (output / "src" / "entry.py").read_text() == "VERSION = 'committed'\n"
    assert not (output / "untracked-secret.env").exists()
    assert not (output / ".git").exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert release.read_release_commit_file(root, sha, "uv.lock") == b"committed-lock\n"
    assert not (tmp_path / "release-snapshot.tar").exists()


def test_split_release_archives_only_the_committed_macos_project(committed, tmp_path):
    root, old_sha = committed["root"], committed["sha"]
    macos = root / "macos"
    (macos / "src").mkdir(parents=True)
    (macos / "pyproject.toml").write_text("[project]\nname='serverfs-mcp'\nversion='0.27.0'\n")
    (macos / "uv.lock").write_text("macos-lock\n")
    (macos / "src" / "hatch_build.py").write_text("BUILD = 'committed'\n")
    (root / "windows").mkdir()
    (root / "windows" / "sentinel").write_text("other platform")
    _git(root, "add", "macos", "windows")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "split projects",
    )
    sha = _git(root, "rev-parse", "HEAD")
    (macos / "uv.lock").write_text("dirty-lock\n")
    output = tmp_path.resolve() / "macos-release"
    assert release.materialize_release_source(macos, sha, output) == output
    assert (output / "uv.lock").read_bytes() == b"macos-lock\n"
    assert (output / "src" / "hatch_build.py").read_bytes() == b"BUILD = 'committed'\n"
    assert not (output / "windows").exists()
    assert not (output / "macos").exists()
    assert release.read_release_commit_file(macos, sha, "uv.lock") == b"macos-lock\n"
    assert release.read_release_commit_file(root, old_sha, "uv.lock") == b"committed-lock\n"

    for source in (root, root / "windows", macos / "src"):
        with pytest.raises(runtime.DarwinSharedRuntimeError):
            release.materialize_release_source(source, sha, tmp_path.resolve() / "wrong-source")
        with pytest.raises(runtime.DarwinSharedRuntimeError):
            release.read_release_commit_file(source, sha, "uv.lock")


@pytest.mark.parametrize("bad", ["HEAD", "0" * 40, "0" * 64, "../HEAD", "a" * 39, "A" * 40])
def test_release_rejects_nonmatching_or_ambiguous_commit(
    committed: dict[str, object], tmp_path: Path, bad: str
) -> None:
    root = committed["root"]
    assert isinstance(root, Path)
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        release.materialize_release_source(root, bad, tmp_path.resolve() / "out")
    assert not (tmp_path / "out").exists()


def test_release_requires_the_requested_commit_to_be_head(
    committed: dict[str, object], tmp_path: Path
) -> None:
    root = committed["root"]
    old_sha = committed["sha"]
    assert isinstance(root, Path) and isinstance(old_sha, str)
    (root / "src" / "entry.py").write_text("VERSION = 'new head'\n", encoding="utf-8")
    _git(root, "add", "src/entry.py")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "advance HEAD",
    )
    assert _git(root, "cat-file", "-t", old_sha) == "commit"
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="HEAD"):
        release.materialize_release_source(root, old_sha, tmp_path.resolve() / "out")
    assert not (tmp_path / "out").exists()


def test_release_refuses_existing_snapshot_and_nested_repo(
    committed: dict[str, object], tmp_path: Path
) -> None:
    root = committed["root"]
    sha = committed["sha"]
    assert isinstance(root, Path) and isinstance(sha, str)
    occupied = tmp_path.resolve() / "existing"
    occupied.mkdir()
    (occupied / "do-not-touch").write_text("protected")
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        release.materialize_release_source(root, sha, occupied)
    assert (occupied / "do-not-touch").read_text() == "protected"
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        release.materialize_release_source(root / "src", sha, tmp_path.resolve() / "wrong-root")


@pytest.mark.parametrize("member_kind", ["traversal", "absolute", "symlink", "hardlink", "fifo"])
def test_release_archive_extraction_rejects_unsafe_tar(tmp_path: Path, member_kind: str) -> None:
    archive = tmp_path.resolve() / "bad.tar"
    with tarfile.open(archive, "w:") as t:
        entry = tarfile.TarInfo(
            "../escape"
            if member_kind == "traversal"
            else "/tmp/escape"
            if member_kind == "absolute"
            else "link"
            if member_kind in {"symlink", "hardlink"}
            else "fifo"
            if member_kind == "fifo"
            else "safe.txt"
        )
        entry.size = 0
        if member_kind == "symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../escape"
        elif member_kind == "hardlink":
            entry.type = tarfile.LNKTYPE
            entry.linkname = "../escape"
        elif member_kind == "fifo":
            entry.type = tarfile.FIFOTYPE
        t.addfile(entry, io.BytesIO(b""))
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        release._extract_archive(archive, tmp_path.resolve() / "out")
    assert not (tmp_path / "out").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_release_tar_rejects_duplicate_file_entries(tmp_path: Path) -> None:
    archive = tmp_path.resolve() / "duplicates.tar"
    with tarfile.open(archive, "w:") as stream:
        for _ in range(2):
            entry = tarfile.TarInfo("pyproject.toml")
            payload = b"[project]\nname='test'\n"
            entry.size = len(payload)
            stream.addfile(entry, io.BytesIO(payload))
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        release._extract_archive(archive, tmp_path.resolve() / "out")


def test_release_tar_collision_preserves_concurrent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path.resolve() / "collision.tar"
    with tarfile.open(archive, "w:") as stream:
        entry = tarfile.TarInfo("unsafe-link")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "../escape"
        stream.addfile(entry)
    destination = tmp_path.resolve() / "out"
    original_mkdir = Path.mkdir

    def race_mkdir(path: Path, *args, **kwargs):
        if path == destination:
            original_mkdir(path, *args, **kwargs)
            (path / "owner-data").write_text("concurrent caller", encoding="utf-8")
            raise FileExistsError("simulated concurrent destination creation")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="extraction failed"):
        release._extract_archive(archive, destination)
    assert (destination / "owner-data").read_text(encoding="utf-8") == "concurrent caller"


@pytest.mark.parametrize(
    "filename",
    ["uv.lock", "pyproject.toml"],
)
def test_release_file_reader_cannot_read_arbitrary_repo_paths(
    committed: dict[str, object], filename: str
) -> None:
    root = committed["root"]
    sha = committed["sha"]
    assert isinstance(root, Path) and isinstance(sha, str)
    value = release.read_release_commit_file(root, sha, filename)
    assert value == (root / filename).read_bytes()
    for forbidden in ("../../.env", "src/entry.py", ".git/config"):
        with pytest.raises(runtime.DarwinSharedRuntimeError, match="not allowed"):
            release.read_release_commit_file(root, sha, forbidden)


def test_release_provenance_schema_and_offline_install_from_commit(
    committed: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = committed["root"]
    sha = committed["sha"]
    assert isinstance(root, Path) and isinstance(sha, str)
    (root / "pyproject.toml").write_text("UNCOMMITTED\n")
    (root / "uv.lock").write_text("UNCOMMITTED-LOCK\n")
    install = tmp_path.resolve() / "install"
    install.mkdir(mode=0o700)
    install.chmod(0o700)
    base = tmp_path / "python-root"
    (base / "bin").mkdir(parents=True)
    (base / "lib").mkdir()
    binary = base / "bin/python3.12"
    binary.write_bytes(b"synthetic-python")
    binary.chmod(0o700)
    dylib = base / "lib" / f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    dylib.write_bytes(b"synthetic-libpython")
    dylib.chmod(0o600)
    uv = tmp_path / "uv"
    uv.write_bytes(b"synthetic-uv")
    uv.chmod(0o700)
    steps: list[str] = []

    def fake_uv(_uv, argv, *, source, step, timeout=150):
        assert (source / "uv.lock").read_bytes() == b"committed-lock\n"
        steps.append(step)
        if step == "lock export":
            assert "--locked" in argv and "--offline" in argv
            Path(argv[argv.index("--output-file") + 1]).write_bytes(b"immutable reqs")
        elif step == "wheel build":
            dist = Path(argv[argv.index("--out-dir") + 1])
            dist.mkdir()
            (dist / "serverfs_mcp-0.25.0-py3-none-any.whl").write_bytes(b"immutable wheel")
        elif step == "venv creation":
            candidate = Path(argv[-1])
            (candidate / "bin").mkdir(parents=True)
            (candidate / "lib").mkdir()
            packages = (
                candidate
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            packages.mkdir(parents=True)
            (packages / "dependency.py").write_bytes(b"dependency = True\n")

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_execute_uv", fake_uv)
    monkeypatch.setattr(runtime, "_publish_new_directory", os.rename)
    monkeypatch.setattr(runtime, "_validate_shared_core_python", lambda p: p)
    monkeypatch.setattr(runtime, "_probe", lambda _p: None)
    target = install / "release-test"
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="version mismatch"):
        runtime.create_shared_core_runtime(
            target, root, binary, uv, release_commit=sha, release_version="0.25.1"
        )
    assert not target.exists()
    result = runtime.create_shared_core_runtime(
        target, root, binary, uv, release_commit=sha, release_version="0.25.0"
    )
    assert result["git_commit"] == sha
    assert steps == [
        "lock export",
        "wheel build",
        "venv creation",
        "locked dependencies",
        "ServerFS wheel",
    ]
    record = json.loads((target / "runtime-provenance.json").read_text())
    assert record["schema_version"] == 3
    assert record["site_packages_file_count"] == 2
    assert record["git_commit"] == sha
    assert record["release_version"] == "0.25.0"
    assert record["uv_lock_sha256"] == hashlib.sha256(b"committed-lock\n").hexdigest()
    assert (
        record["pyproject_sha256"]
        == hashlib.sha256(release.read_release_commit_file(root, sha, "pyproject.toml")).hexdigest()
    )
    assert runtime.verify_shared_core_runtime(target, project_root=root)["git_commit"] == sha
    assert not any(p.name.endswith(".tar") for p in install.rglob("*"))
    assert (
        runtime.main(
            [
                "--verify-only",
                "--project-root",
                str(root),
                "--target",
                str(target),
                "--release-commit",
                sha,
            ]
        )
        == 0
    )
    assert (
        runtime.main(
            [
                "--verify-only",
                "--project-root",
                str(root),
                "--target",
                str(target),
                "--release-commit",
                "0" * 40,
            ]
        )
        == 1
    )
    # A private record that claims another Git object must fail validation,
    # regardless of whether the local checkout has unrelated dirty files.
    provenance = target / "runtime-provenance.json"
    record["git_commit"] = "0" * 40
    provenance.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        runtime.verify_shared_core_runtime(target, project_root=root)
    record["git_commit"] = sha
    record["uv_lock_sha256"] = "f" * 64
    provenance.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="metadata drift"):
        runtime.verify_shared_core_runtime(target, project_root=root)
