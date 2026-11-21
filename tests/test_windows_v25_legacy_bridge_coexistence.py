"""v25 independent writable Bridge coexistence must prove legacy roots are disjoint.

No native service, process, task, or real legacy credentials are accessed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import bridge_v2_owner, cli
from serverfs_mcp.windows_v25 import legacy_bridge_ownership as legacy
from serverfs_mcp.windows_v25.workdir_ownership import WorkdirClaim

_TEST_PRIVATE = (
    r"C:\Users\owner\AppData\Local\ServerFS"
    r"\v25\instances\i5-shared\workdirs\fixture"
)
_LEGACY = r"D:\Projects\Legacy"


def _candidate() -> SimpleNamespace:
    return SimpleNamespace(component="bridge", instance_id="i5-shared")


def _patch_proposed(monkeypatch, *, writable: bool = True) -> None:
    monkeypatch.setattr(
        bridge_v2_owner,
        "load_owned_bridge_workdir_claims",
        lambda _manifest: (WorkdirClaim("i5-shared", _TEST_PRIVATE, writable),),
    )


def test_running_legacy_can_coexist_with_disjoint_private_writable_bridge(monkeypatch) -> None:
    _patch_proposed(monkeypatch)
    active_calls = []

    def running():
        active_calls.append(1)
        return True

    monkeypatch.setattr(cli, "_legacy_bridge_active", running)
    monkeypatch.setattr(
        legacy,
        "verified_legacy_claims",
        lambda: (WorkdirClaim("default", _LEGACY, True),),
    )
    legacy.assert_legacy_writable_roots_disjoint(_candidate())
    assert len(active_calls) == 2


@pytest.mark.parametrize(
    "overlap",
    [
        _TEST_PRIVATE,
        r"C:\Users\owner\AppData\Local\ServerFS",
        _TEST_PRIVATE + r"\nested",
    ],
)
def test_legacy_overlap_always_rejects_writable_bridge(monkeypatch, overlap: str) -> None:
    _patch_proposed(monkeypatch)
    monkeypatch.setattr(cli, "_legacy_bridge_active", lambda: True)
    monkeypatch.setattr(
        legacy,
        "verified_legacy_claims",
        lambda: (WorkdirClaim("default", overlap, False),),
    )
    with pytest.raises(legacy.LegacyBridgeOwnerError):
        legacy.assert_legacy_writable_roots_disjoint(_candidate())


def test_unverifiable_legacy_policy_rejects_rather_than_skipping_gate(monkeypatch) -> None:
    _patch_proposed(monkeypatch)
    monkeypatch.setattr(cli, "_legacy_bridge_active", lambda: True)

    def unverified():
        raise ValueError("unverified policy")

    monkeypatch.setattr(legacy, "verified_legacy_claims", unverified)
    with pytest.raises(legacy.LegacyBridgeOwnerError, match="cannot be excluded"):
        legacy.assert_legacy_writable_roots_disjoint(_candidate())


def test_legacy_task_generation_changes_mid_read_fails_closed(monkeypatch) -> None:
    _patch_proposed(monkeypatch)
    states = iter([True, False])
    monkeypatch.setattr(cli, "_legacy_bridge_active", lambda: next(states))
    monkeypatch.setattr(
        legacy,
        "verified_legacy_claims",
        lambda: (WorkdirClaim("default", _LEGACY, False),),
    )
    with pytest.raises(legacy.LegacyBridgeOwnerError):
        legacy.assert_legacy_writable_roots_disjoint(_candidate())


def test_non_running_legacy_does_not_require_an_active_owner_policy(monkeypatch) -> None:
    _patch_proposed(monkeypatch)
    monkeypatch.setattr(cli, "_legacy_bridge_active", lambda: False)

    def should_not_load():
        raise AssertionError("inactive legacy policy must not be required")

    monkeypatch.setattr(legacy, "verified_legacy_claims", should_not_load)
    legacy.assert_legacy_writable_roots_disjoint(_candidate())


def test_readonly_new_bridge_does_not_claim_write_ownership(monkeypatch) -> None:
    _patch_proposed(monkeypatch, writable=False)

    def should_not_query():
        raise AssertionError("readonly Bridge should never demand legacy query")

    monkeypatch.setattr(cli, "_legacy_bridge_active", should_not_query)
    legacy.assert_legacy_writable_roots_disjoint(_candidate())


@pytest.mark.parametrize(
    "component,instance",
    [("core", "i5-shared"), ("bridge", "default")],
)
def test_only_nondefault_bridge_is_eligible(monkeypatch, component, instance) -> None:
    with pytest.raises(legacy.LegacyBridgeOwnerError):
        legacy.assert_legacy_writable_roots_disjoint(
            SimpleNamespace(component=component, instance_id=instance)
        )


def test_verified_legacy_claims_requires_authenticated_task_xml(monkeypatch) -> None:
    from serverfs_mcp import native_endpoint, windows_service

    sid = "S-1-5-21-100-200-300-1001"
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: sid)
    monkeypatch.setattr(
        windows_service,
        "_manifest_document",
        lambda: {
            "schema_version": 1,
            "task_name": windows_service.TASK_NAME,
            "user_sid": sid,
            "config_path": r"D:\Configs\serverfs.toml",
        },
    )
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda: "task XML")
    monkeypatch.setattr(
        windows_service,
        "validate_registered_task",
        lambda xml, document: (False, "not valid"),
    )
    with pytest.raises(legacy.LegacyBridgeOwnerError):
        legacy.verified_legacy_claims()


def test_windows_cli_and_service_both_require_verified_legacy_overlap_gate() -> None:
    source_cli = (
        Path(__file__).resolve().parents[1] / "src/serverfs_mcp/windows_v25/cli.py"
    ).read_text("utf-8")
    source_runtime = (
        Path(__file__).resolve().parents[1] / "src/serverfs_mcp/windows_v25/runtime.py"
    ).read_text("utf-8")
    assert "assert_legacy_writable_roots_disjoint(manifest)" in source_cli
    assert "assert_legacy_writable_roots_disjoint(manifest)" in source_runtime
    assert "legacy Bridge write ownership must be retired first" not in source_cli
    assert "legacy Bridge write ownership is still active" not in source_runtime
