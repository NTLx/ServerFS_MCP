"""macOS named owned unstage retains any running or unexpected state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_owned_unstage as cleanup
from serverfs_mcp.instance_identity import InstanceIdentity


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    identity = InstanceIdentity(
        instance_id="owned-clean", bridge_mode="owned", bridge_id="owned-clean"
    )
    instances = tmp_path / "Library/Application Support/ServerFS/instances"
    home = instances / identity.instance_id
    root = home / "service-manager"
    bridge = root / "data/agent-bridge"
    for path in (
        instances,
        home,
        root,
        root / "logs",
        root / "data",
        bridge,
        bridge / "state",
        bridge / "locks",
        root / "ServerFSServiceManager.app",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": identity.instance_id,
                "bridge_mode": "owned",
                "bridge_id": identity.instance_id,
                "core": {"port": 19283},
                "services": {"agent-bridge": {}, "http-origin": {}},
            }
        )
    )
    snapshot = root / "http-origin-environment.json"
    snapshot.write_text("{}")
    private = bridge / "bridge.json"
    private.write_text("{}")
    for path in (manifest, snapshot, private):
        path.chmod(0o600)
    state = {"bridge": "not-registered", "core": "not-registered"}
    monkeypatch.setattr(cleanup, "_status", lambda _, component, __: state[component])
    monkeypatch.setattr(cleanup.darwin_service, "_launchd_job_pid", lambda _: None)
    monkeypatch.setattr(
        cleanup,
        "named_owned_bridge_resources",
        lambda *_args, **_kwargs: type(
            "Resources", (), {"socket_path": tmp_path / "not-created" / "bridge.sock"}
        )(),
    )
    from serverfs_mcp import darwin_instance_runtime

    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager_path",
        lambda *_args, **_kwargs: root / "ServerFSServiceManager.app",
    )
    return identity, root, home, state


def test_owned_clean_inactive_candidate_can_be_unstaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, root, owner, _ = _case(tmp_path, monkeypatch)
    peer = root.parent.parent / "other-instance"
    peer.mkdir()
    result = cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert result == {"instance_id": "owned-clean", "state": "unstaged"}
    assert not root.exists() and not owner.exists()
    assert peer.exists()


@pytest.mark.parametrize(
    "extra",
    ["log", "state", "unknown", "locks", "socket"],
)
def test_owned_unstage_refuses_unexpected_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    identity, root, _, _ = _case(tmp_path, monkeypatch)
    if extra == "log":
        (root / "logs/core.stderr.log").write_text("runtime activity")
    elif extra == "state":
        (root / "data/agent-bridge/state/tasks.db").write_text("private state")
    elif extra == "locks":
        (root / "data/agent-bridge/locks/lease").write_text("lease")
    elif extra == "unknown":
        (root / "operator-note.txt").write_text("retain me")
    else:
        (tmp_path / "not-created").mkdir(mode=0o700)
    with pytest.raises(cleanup.DarwinOwnedUnstageError):
        cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert root.exists() and (root / "services.json").is_file()


def test_owned_unstage_refuses_registered_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, root, _, statuses = _case(tmp_path, monkeypatch)
    statuses["bridge"] = "enabled"
    with pytest.raises(cleanup.DarwinOwnedUnstageError, match="unregistered"):
        cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert root.is_dir()


def test_owned_unstage_refuses_active_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    identity, root, _, _ = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(cleanup.darwin_service, "_launchd_job_pid", lambda _: 93213)
    with pytest.raises(cleanup.DarwinOwnedUnstageError, match="process remains"):
        cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert root.is_dir()


@pytest.mark.parametrize("service", ["cloudflare.blue", "tunnel.blue"])
@pytest.mark.parametrize("active", [False, True])
def test_owned_unstage_refuses_registered_or_active_dynamic_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    active: bool,
) -> None:
    identity, root, _, _ = _case(tmp_path, monkeypatch)
    manifest = json.loads((root / "services.json").read_text())
    manifest["services"][service] = {}
    (root / "services.json").write_text(json.dumps(manifest))
    (root / "services.json").chmod(0o600)
    pid = 9861 if active else None
    monkeypatch.setattr(cleanup.darwin_service, "_launchd_job_pid", lambda _label: pid)
    monkeypatch.setattr(
        cleanup,
        "_manager",
        lambda _identity, operation, key, **_kwargs: "not-registered" if active else "enabled",
    )
    with pytest.raises(cleanup.DarwinOwnedUnstageError, match="Connector|process remains"):
        cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert root.is_dir() and (root / "services.json").is_file()


@pytest.mark.parametrize("service", ["cloudflare..blue", "tunnel.BLUE", "unknown.blue"])
def test_owned_unstage_refuses_malformed_or_unknown_manifest_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    identity, root, _, _ = _case(tmp_path, monkeypatch)
    manifest = json.loads((root / "services.json").read_text())
    manifest["services"][service] = {}
    (root / "services.json").write_text(json.dumps(manifest))
    (root / "services.json").chmod(0o600)
    with pytest.raises(cleanup.DarwinOwnedUnstageError, match="untrusted"):
        cleanup.unstage_owned_candidate(identity, home=tmp_path)
    assert root.is_dir() and (root / "services.json").is_file()


def test_owned_unstage_rejects_default_or_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="independently named"):
        cleanup.unstage_owned_candidate(InstanceIdentity(), home=tmp_path)
