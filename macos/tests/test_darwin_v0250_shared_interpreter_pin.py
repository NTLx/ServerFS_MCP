"""Signed bundle pinning for named shared Core interpreter selection."""

from __future__ import annotations

import plistlib
import shutil
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instances
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_instances import _source_app


def _shared_identity() -> InstanceIdentity:
    return InstanceIdentity(instance_id="shared", bridge_mode="shared", bridge_id="default")


def _python_copy(path: Path) -> Path:
    target = path / "python"
    shutil.copy2(sys.executable, target)
    target.chmod(0o700)
    return target


def _stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python: Path | None
) -> tuple[Path, list[Path]]:
    source = _source_app(tmp_path)
    destination_dir = tmp_path / "output"
    destination_dir.mkdir()
    calls: list[Path] = []
    monkeypatch.setattr(darwin_instances, "_sign_and_verify", lambda app: calls.append(app))
    kwargs = {} if python is None else {"shared_core_python": python}
    destination = destination_dir / darwin_instances.APP_NAME
    darwin_instances.stage_instance_app(_shared_identity(), source, destination, **kwargs)
    return destination, calls


def test_shared_stage_requires_opt_in_and_writes_signed_bundle_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = _python_copy(tmp_path)
    destination, calls = _stage(tmp_path, monkeypatch, python)
    assert len(calls) == 1
    info = plistlib.loads((destination / "Contents/Info.plist").read_bytes())
    assert info["ServerFSSharedCorePythonPath"] == str(python)
    agents = destination / "Contents/Library/LaunchAgents"
    assert not (agents / f"{_shared_identity().macos_label('bridge')}.plist").exists()


def test_shared_stage_without_pin_fails_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_app(tmp_path)
    target = tmp_path / "output" / darwin_instances.APP_NAME
    target.parent.mkdir()
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError, match="explicit Core Python pin"):
        darwin_instances.stage_instance_app(_shared_identity(), source, target)
    assert not target.exists()


@pytest.mark.parametrize("unsafe", ["group-write", "other-write", "symlink"])
def test_unsafe_pin_fails_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    python = _python_copy(tmp_path)
    if unsafe == "group-write":
        python.chmod(0o720)
    elif unsafe == "other-write":
        python.chmod(0o702)
    else:
        link = tmp_path / "python-link"
        link.symlink_to(python)
        python = link
    source = _source_app(tmp_path)
    target = tmp_path / "output" / darwin_instances.APP_NAME
    target.parent.mkdir()
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError):
        darwin_instances.stage_instance_app(
            _shared_identity(), source, target, shared_core_python=python
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity(instance_id="files", bridge_mode="disabled"),
        InstanceIdentity(instance_id="owned", bridge_mode="owned", bridge_id="owned"),
    ],
)
def test_nonshared_modes_reject_python_pin_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: InstanceIdentity
) -> None:
    source = _source_app(tmp_path)
    target = tmp_path / "output" / darwin_instances.APP_NAME
    target.parent.mkdir()
    python = _python_copy(tmp_path)
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError, match="only valid for shared"):
        darwin_instances.stage_instance_app(identity, source, target, shared_core_python=python)
    assert not target.exists()


def test_launcher_requires_bundle_pin_equal_to_shared_manifest_executable() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "native/macos/service_manager/Sources/ServiceLauncher/main.swift").read_text()
    assert 'info["ServerFSSharedCorePythonPath"] as? String' in source
    assert "!pinnedPython.isEmpty" in source
    assert "pinnedPython == gate.service.executable" in source
    assert (
        'throw LauncherError.invalidManifest("shared Core Python pin is missing or mismatched")'
        in source
    )
    assert "SHA256.hash(data: configData)" in source
    assert "requirePrivateRegularFile(environmentURL" in source
    assert "getuid()" in source
