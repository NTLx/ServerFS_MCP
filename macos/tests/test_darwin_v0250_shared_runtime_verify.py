"""Read-only verification of a pinned macOS Shared Core runtime.

These tests use synthetic bytes, never invoke uv, sign an app, or register a
service. The separate opt-in E2E proves the real interpreter import probe.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_shared_runtime as runtime


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_probe", lambda _target: None)
    base = tmp_path.resolve()
    target = base / "standalone-core"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    bin_dir = target / "bin"
    bin_dir.mkdir()
    lib_dir = target / "lib"
    lib_dir.mkdir()
    packages = (
        target
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    packages.mkdir(parents=True)
    package_file = packages / "sample.py"
    package_file.write_bytes(b"value = 1\n")
    inventory = runtime._site_packages_inventory(target)
    pinned = bin_dir / "serverfs-core-python"
    python_data = b"synthetic executable bytes"
    pinned.write_bytes(python_data)
    pinned.chmod(0o700)
    dylib = lib_dir / f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    lib_data = b"synthetic matching macOS dynamic library"
    dylib.write_bytes(lib_data)
    dylib.chmod(0o600)
    project = base / "source"
    project.mkdir()
    lock = project / "uv.lock"
    lock.write_text("synthetic frozen lock data", encoding="utf-8")
    pyproject = project / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'synthetic'\n", encoding="utf-8")
    document = {
        "schema_version": 3,
        "offline": True,
        "uv_lock_sha256": _sha(lock.read_bytes()),
        "pyproject_sha256": _sha(pyproject.read_bytes()),
        "requirements_sha256": _sha(b"synthetic lock export"),
        "wheel_sha256": _sha(b"synthetic wheel"),
        "python_sha256": _sha(python_data),
        "libpython_sha256": _sha(lib_data),
        "python_name": pinned.name,
        "libpython_name": dylib.name,
        "site_packages_sha256": inventory[0],
        "site_packages_file_count": inventory[1],
        "site_packages_bytes": inventory[2],
    }
    provenance = target / "runtime-provenance.json"
    provenance.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    provenance.chmod(0o600)
    return {
        "target": target,
        "python": pinned,
        "dylib": dylib,
        "lock": lock,
        "pyproject": pyproject,
        "project": project,
        "record": provenance,
        "document": document,
        "package_file": package_file,
    }


def test_official_managed_python_requires_provenance(installed) -> None:
    assert runtime.verify_managed_shared_core_python(installed["python"]) is True
    assert (
        runtime.verify_managed_shared_core_python(
            installed["python"].with_name("development-python")
        )
        is False
    )
    installed["dylib"].write_bytes(b"mutated libpython after staging")
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="integrity"):
        runtime.verify_managed_shared_core_python(installed["python"])


def test_managed_filename_outside_bin_is_rejected_without_probing(
    installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = installed["target"] / "serverfs-core-python"
    monkeypatch.setattr(runtime, "verify_shared_core_runtime", lambda *_a: pytest.fail("no probe"))
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="path"):
        runtime.verify_managed_shared_core_python(outside)


def test_verify_runtime_preserves_existing_files(installed) -> None:
    c = installed
    before = {
        name: path.read_bytes()
        for name, path in (
            ("python", c["python"]),
            ("libpython", c["dylib"]),
            ("provenance", c["record"]),
            ("lock", c["lock"]),
        )
    }
    result = runtime.verify_shared_core_runtime(c["target"], project_root=c["project"])
    assert result == {
        "state": "verified",
        "python": str(c["python"]),
        "uv_lock_sha256": c["document"]["uv_lock_sha256"],
        "wheel_sha256": c["document"]["wheel_sha256"],
    }
    assert all(
        path.read_bytes() == before[name]
        for name, path in (
            ("python", c["python"]),
            ("libpython", c["dylib"]),
            ("provenance", c["record"]),
            ("lock", c["lock"]),
        )
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "python-data",
        "libpython-data",
        "libpython-group-write",
        "libpython-symlink",
        "record-world-readable",
        "record-symlink",
        "record-unknown-field",
        "record-duplicate-field",
        "record-truncated",
        "lock-drift",
        "project-drift",
        "runtime-not-private",
        "runtime-symlink",
        "package-bytes",
        "package-symlink",
        "rogue-code-file",
        "missing-package-file",
        "injected-bytecode-cache",
        "nonregular-package-entry",
    ],
)
def test_verify_rejects_drift_without_mutating_a_runtime(
    installed, tmp_path: Path, tamper: str
) -> None:
    c = installed
    target = c["target"]
    if tamper == "python-data":
        c["python"].write_bytes(b"altered interpreter")
    elif tamper == "libpython-data":
        c["dylib"].write_bytes(b"altered libpython")
    elif tamper == "libpython-group-write":
        c["dylib"].chmod(0o660)
    elif tamper == "libpython-symlink":
        old = c["dylib"].with_name("safe-lib-original")
        c["dylib"].rename(old)
        c["dylib"].symlink_to(old.name)
    elif tamper == "record-world-readable":
        c["record"].chmod(0o644)
    elif tamper == "record-symlink":
        old = c["record"].with_name("saved-provenance.json")
        c["record"].rename(old)
        c["record"].symlink_to(old.name)
    elif tamper == "record-unknown-field":
        c["document"]["unexpected"] = "payload"
        c["record"].write_text(json.dumps(c["document"]))
    elif tamper == "record-duplicate-field":
        existing = c["record"].read_text()
        c["record"].write_text(existing[:-1] + ', "offline": true}')
    elif tamper == "record-truncated":
        c["record"].write_text("{")
    elif tamper == "lock-drift":
        c["lock"].write_text("changed frozen lock")
    elif tamper == "project-drift":
        c["pyproject"].write_text("changed project metadata")
    elif tamper == "runtime-not-private":
        target.chmod(0o755)
    elif tamper == "runtime-symlink":
        alias = tmp_path.resolve() / "runtime-alias"
        alias.symlink_to(target, target_is_directory=True)
        target = alias
    elif tamper == "package-bytes":
        c["package_file"].write_bytes(b"value = 2\n")
    elif tamper == "package-symlink":
        original = c["package_file"].with_name("original.py")
        c["package_file"].rename(original)
        c["package_file"].symlink_to(original.name)
    elif tamper == "rogue-code-file":
        c["package_file"].with_name("rogue.py").write_bytes(b"raise RuntimeError()\n")
    elif tamper == "missing-package-file":
        c["package_file"].unlink()
    elif tamper == "injected-bytecode-cache":
        cache = c["package_file"].parent / "__pycache__"
        cache.mkdir()
        (cache / "sample.cpython-312.pyc").write_bytes(b"malicious executable bytecode")
    elif tamper == "nonregular-package-entry":
        os.mkfifo(c["package_file"].with_name("unexpected.fifo"))
    with pytest.raises(runtime.DarwinSharedRuntimeError):
        runtime.verify_shared_core_runtime(target, project_root=c["project"])


def test_verify_only_cli_reads_runtime_without_installing(
    installed, capsys: pytest.CaptureFixture[str]
) -> None:
    c = installed
    assert (
        runtime.main(
            [
                "--verify-only",
                "--target",
                str(c["target"]),
                "--project-root",
                str(c["project"]),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "verified"
    assert payload["python"] == str(c["python"])


def test_install_cli_still_requires_source_python_and_uv(
    installed, capsys: pytest.CaptureFixture[str]
) -> None:
    c = installed
    with pytest.raises(SystemExit) as error:
        runtime.main(
            [
                "--target",
                str(c["target"]),
                "--project-root",
                str(c["project"]),
            ]
        )
    assert error.value.code == 2
    assert "--source-python and --uv" in capsys.readouterr().err


def test_verify_checks_installed_site_packages_integrity(installed) -> None:
    result = runtime.verify_shared_core_runtime(
        installed["target"], project_root=installed["project"]
    )
    assert result["state"] == "verified"
    assert stat.S_IMODE(installed["record"].stat().st_mode) == 0o600


@pytest.mark.parametrize("schema_version", [1, 2])
def test_historical_schema_remains_readable_but_is_not_managed_admissible(
    installed, schema_version: int
) -> None:
    c = installed
    for key in ("site_packages_sha256", "site_packages_file_count", "site_packages_bytes"):
        c["document"].pop(key)
    c["document"]["schema_version"] = schema_version
    if schema_version == 2:
        c["document"]["git_commit"] = "a" * 40
        c["document"]["release_version"] = "0.24.2"
    c["record"].write_text(json.dumps(c["document"], sort_keys=True), encoding="utf-8")
    assert runtime.verify_shared_core_runtime(c["target"])["state"] == "verified"
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="lacks site-packages integrity"):
        runtime.verify_managed_shared_core_python(c["python"])


def test_v3_rejects_mixed_release_schema(installed) -> None:
    c = installed
    c["document"]["git_commit"] = "a" * 40
    c["record"].write_text(json.dumps(c["document"], sort_keys=True), encoding="utf-8")
    with pytest.raises(runtime.DarwinSharedRuntimeError, match="schema"):
        runtime.verify_shared_core_runtime(c["target"], project_root=c["project"])
