"""Legacy default Bridge workdir claims use the native startup contract.

The historical default Bridge uses the Core's TOML file (possibly in a
checkout), not the private JSON policy of a nondefault v25 Bridge. Keep
canonical paths and overlap denial without imposing a new private DACL on
the retired/dormant default instance.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import bridge_v2_owner, storage
from serverfs_mcp.windows_v25.spec import Manifest
from serverfs_mcp.windows_v25.workdir_ownership import (
    WorkdirClaim,
    WorkdirOwnershipError,
    assert_independent_bridge_roots,
)

SID = "S-1-5-21-111-222-333-1001"


def _default_manifest(tmp_path: Path) -> Manifest:
    return Manifest.parse(
        {
            "schema": 1,
            "component": "bridge",
            "sid": SID,
            "pythonw": str(tmp_path / "pythonw.exe"),
            "settings": {
                "config": str(tmp_path / "repo" / "serverfs.toml"),
                "bridge_python": str(tmp_path / "bridge" / "python.exe"),
            },
        }
    )


def test_default_claims_follow_native_root_validation_not_private_json_acl(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_mcp import native_config
    from serverfs_mcp.windows_v25 import runtime

    default = _default_manifest(tmp_path)
    config_path = Path(str(default.settings["config"]))
    roots = [tmp_path / "repo" / "docs", tmp_path / "data"]
    for root in roots:
        root.mkdir(parents=True)
    visits = []

    def native_roots(path):
        visits.append(("root_check", path))
        return config_path, roots

    def native_policy(path):
        visits.append(("native_config", path))
        return [
            SimpleNamespace(root=roots[0], read_only=True),
            SimpleNamespace(root=roots[1], read_only=False),
        ], None

    monkeypatch.setattr(runtime, "_roots", native_roots)
    monkeypatch.setattr(native_config, "load_native_config", native_policy)
    monkeypatch.setattr(
        storage,
        "check_private",
        lambda *a, **k: pytest.fail("legacy TOML must not acquire the v25 private JSON DACL"),
    )

    claims = bridge_v2_owner.load_owned_bridge_workdir_claims(default)
    assert claims == (
        WorkdirClaim("default", str(roots[0]), False),
        WorkdirClaim("default", str(roots[1]), True),
    )
    assert visits == [("root_check", config_path), ("native_config", config_path)]


def test_default_policy_cannot_mismatch_checked_native_roots(tmp_path: Path, monkeypatch):
    from serverfs_mcp import native_config
    from serverfs_mcp.windows_v25 import runtime

    default = _default_manifest(tmp_path)
    monkeypatch.setattr(runtime, "_roots", lambda path: (path, [tmp_path / "one"]))
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda path: (
            [
                SimpleNamespace(root=tmp_path / "one", read_only=True),
                SimpleNamespace(root=tmp_path / "two", read_only=True),
            ],
            None,
        ),
    )
    with pytest.raises(bridge_v2_owner.BridgeV2OwnerError, match="inconsistent"):
        bridge_v2_owner.load_owned_bridge_workdir_claims(default)


def test_default_policy_root_reordering_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import native_config
    from serverfs_mcp.windows_v25 import runtime

    default = _default_manifest(tmp_path)
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(runtime, "_roots", lambda path: (path, roots))
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda path: (
            [
                SimpleNamespace(root=roots[1], read_only=False),
                SimpleNamespace(root=roots[0], read_only=True),
            ],
            None,
        ),
    )
    with pytest.raises(bridge_v2_owner.BridgeV2OwnerError, match="inconsistent"):
        bridge_v2_owner.load_owned_bridge_workdir_claims(default)


def test_default_overlap_with_writable_bridge_remains_refused(tmp_path: Path):
    base = tmp_path / "repo"
    old = (WorkdirClaim("default", str(base), True),)
    proposed = (WorkdirClaim("i2-smoke", str(base / "fixture"), False),)
    with pytest.raises(WorkdirOwnershipError, match="overlap"):
        assert_independent_bridge_roots(proposed, old)
    disjoint = (WorkdirClaim("i2-smoke", str(tmp_path / "isolated"), False),)
    assert assert_independent_bridge_roots(disjoint, old) is None
