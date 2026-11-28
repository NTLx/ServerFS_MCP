"""Nondefault Bridge owner lease and disabled Scheduler XML, pure source tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.native_lifecycle import LifecycleLease, LifecycleOwnershipError
from serverfs_mcp.windows_component_task import build_xml, validate_registered
from serverfs_mcp.windows_v25 import storage
from serverfs_mcp.windows_v25.tasks import _enabled, _task_spec
from test_windows_v25_bridge_v2_owner import _fixture, _plan


def test_v25_bridge_lease_uses_own_private_root_and_sid_bridge_digest(tmp_path: Path):
    plan = _plan(_fixture(tmp_path, "bridge-a"))
    lease = LifecycleLease(root=tmp_path / "private-a", artifact_name=plan.lease_artifact)
    assert lease.path.parent == tmp_path / "private-a"
    assert lease.path.name == plan.lease_artifact
    other = _plan(_fixture(tmp_path, "bridge-b"))
    assert other.lease_artifact != plan.lease_artifact
    assert LifecycleLease(root=tmp_path / "private-b").path.name != ""


@pytest.mark.parametrize(
    "unsafe",
    [
        "../escape",
        "serverfs-agent-lifecycle-evil.lifecycle",
        "serverfs-agent-lifecycle-" + "G" * 32 + ".lifecycle",
        "serverfs-agent-lifecycle-" + "a" * 31 + ".lifecycle",
        "serverfs-agent-lifecycle-" + "a" * 33 + ".lifecycle",
    ],
)
def test_custom_lease_identity_refuses_invalid_or_path_redirect(unsafe, tmp_path):
    with pytest.raises(LifecycleOwnershipError):
        LifecycleLease(root=tmp_path, artifact_name=unsafe)


def test_nondefault_bridge_scheduler_definition_is_independent_and_disabled(
    tmp_path: Path, monkeypatch
):
    owner = _fixture(tmp_path, "bridge-a")[0]
    other = _fixture(tmp_path, "bridge-b")[0]
    monkeypatch.setattr(
        storage,
        "pinned_pythonw",
        lambda component, instance_id="default": (
            tmp_path
            / "v25"
            / "instances"
            / instance_id
            / "envs"
            / component
            / "Scripts"
            / "pythonw.exe"
        ),
    )
    for manifest in (owner, other):
        spec = _task_spec(manifest)
        assert spec.name == manifest.task_name()
        assert spec.enabled is False
        assert spec.arguments == manifest.task_argv()
        text = build_xml(spec)
        valid, _reason = validate_registered(text.decode("utf-16"), spec)
        assert valid
        assert not _enabled(text.decode("utf-16"))
    assert owner.task_name() != other.task_name()
