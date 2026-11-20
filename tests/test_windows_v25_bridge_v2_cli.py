"""Nondefault Windows Bridge Scheduler orchestration is instance-scoped.

Every test uses fake inventory, plan, Scheduler and legacy status; no native
process, task, Job, Named Pipe or external credential is accessed.
"""

from __future__ import annotations

import pytest

from serverfs_mcp.windows_v25 import bridge_v2_owner, cli, legacy_bridge_ownership, storage
from serverfs_mcp.windows_v25.legacy_bridge_ownership import LegacyBridgeOwnerError
from serverfs_mcp.windows_v25.workdir_ownership import WorkdirClaim
from test_windows_v25_bridge_v2_owner import _fixture


def test_cli_bridge_isolation_uses_peer_manifests_and_overlap_rules(tmp_path, monkeypatch):
    candidate = _fixture(tmp_path, "bridge-a")[0]
    peer = _fixture(tmp_path, "bridge-b")[0]
    called = []

    def claims(manifest):
        called.append(manifest.instance_id)
        return (
            WorkdirClaim(
                manifest.instance_id,
                r"C:\Research" if manifest.instance_id == "bridge-a" else r"C:\Research\Nested",
                True,
            ),
        )

    monkeypatch.setattr(bridge_v2_owner, "load_owned_bridge_workdir_claims", claims)
    monkeypatch.setattr(storage, "component_inventory", lambda component: (candidate, peer))
    with pytest.raises(ValueError, match="overlap"):
        cli._assert_instance_isolation(candidate)
    assert called == ["bridge-a", "bridge-b"]

    def readonly(manifest):
        return (WorkdirClaim(manifest.instance_id, r"C:\Research", False),)

    monkeypatch.setattr(bridge_v2_owner, "load_owned_bridge_workdir_claims", readonly)
    cli._assert_instance_isolation(candidate)


def _dispatch_start(tmp_path, monkeypatch, *, writable, legacy_active, legacy_root=r"D:\Disjoint"):
    manifest = _fixture(tmp_path, "bridge-a")[0]
    monkeypatch.setattr(storage, "load", lambda component, instance_id: manifest)
    monkeypatch.setattr(cli, "_preflight", lambda _: None)
    monkeypatch.setattr(cli, "_assert_instance_isolation", lambda _: None)
    monkeypatch.setattr(
        bridge_v2_owner,
        "load_owned_bridge_workdir_claims",
        lambda _: (WorkdirClaim("bridge-a", r"C:\Research", writable),),
    )
    # Source-only tests must not inspect the host Legacy manifest, XML or disk.
    # The product permits verified disjoint writable owners, not a global veto.
    monkeypatch.setattr(
        legacy_bridge_ownership,
        "verified_legacy_claims",
        lambda: (WorkdirClaim("default", legacy_root, True),),
    )
    called = []

    def legacy():
        called.append("legacy")
        if legacy_active is None:
            raise AssertionError("no legacy query is allowed for readonly v2")
        return legacy_active

    class FakeScheduler:
        def start(self, _manifest):
            called.append("started")
            return {"running": True}

    monkeypatch.setattr(cli, "_legacy_bridge_active", legacy)
    monkeypatch.setattr(cli, "Scheduler", FakeScheduler)
    args = cli.parser().parse_args(["bridge", "--instance", "bridge-a", "start"])
    return args, called


def test_readonly_v2_owner_start_never_queries_or_stops_legacy(tmp_path, monkeypatch):
    args, called = _dispatch_start(tmp_path, monkeypatch, writable=False, legacy_active=None)
    assert cli.dispatch(args)["running"] is True
    assert called == ["started"]


def test_writable_v2_owner_refuses_overlapping_legacy_before_scheduler(tmp_path, monkeypatch):
    args, called = _dispatch_start(
        tmp_path,
        monkeypatch,
        writable=True,
        legacy_active=True,
        legacy_root=r"C:\Research",
    )
    with pytest.raises(LegacyBridgeOwnerError, match="legacy Bridge writable"):
        cli.dispatch(args)
    assert called == ["legacy"]


def test_writable_v2_owner_allows_verified_disjoint_legacy(tmp_path, monkeypatch):
    args, called = _dispatch_start(tmp_path, monkeypatch, writable=True, legacy_active=True)
    assert cli.dispatch(args)["running"] is True
    assert called == ["legacy", "legacy", "started"]


def test_writable_v2_owner_fails_closed_when_legacy_claim_unverifiable(tmp_path, monkeypatch):
    args, called = _dispatch_start(tmp_path, monkeypatch, writable=True, legacy_active=True)

    def unavailable():
        raise OSError("synthetic Legacy policy is unavailable")

    monkeypatch.setattr(legacy_bridge_ownership, "verified_legacy_claims", unavailable)
    with pytest.raises(LegacyBridgeOwnerError, match="legacy Bridge writable"):
        cli.dispatch(args)
    assert called == ["legacy"]


def test_writable_v2_owner_may_start_when_legacy_is_verified_dormant(tmp_path, monkeypatch):
    args, called = _dispatch_start(tmp_path, monkeypatch, writable=True, legacy_active=False)
    assert cli.dispatch(args)["running"] is True
    assert called == ["legacy", "started"]
