"""Authenticate a running legacy Windows Bridge's workdir roots before coexistence.

A global "legacy task Running" veto prevents disjoint writable Bridge
instances, contrary to v0.25's independent-owner model. Instead, permit
nondefault writable Bridge startup ONLY after verifying the legacy Scheduler
XML, current-user manifest and securely resolved native TOML workdir roots,
then checking both owners' canonical WorkdirClaims for intersection.

This does not mutate legacy services, acquire their owner lease or weaken
the same-Bridge identity prohibition. Any unverified state denies startup.
"""

from __future__ import annotations

from pathlib import Path

from .spec import Manifest
from .workdir_ownership import WorkdirClaim, assert_independent_bridge_roots


class LegacyBridgeOwnerError(ValueError):
    """Sanitized failure to exclude a cross-generation writable collision."""


def _deny() -> LegacyBridgeOwnerError:
    return LegacyBridgeOwnerError("legacy Bridge writable ownership cannot be excluded")


def verified_legacy_claims() -> tuple[WorkdirClaim, ...]:
    """Read exact active legacy task identity and canonical effective native policy."""
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_service import (
        TASK_NAME,
        _manifest_document,
        _query_task_xml,
        validate_registered_task,
    )

    from .runtime import _roots

    try:
        manifest = _manifest_document()
        if (
            manifest.get("schema_version") != 1
            or manifest.get("task_name") != TASK_NAME
            or manifest.get("user_sid") != current_user_sid()
        ):
            raise _deny()
        xml = _query_task_xml()
        if not isinstance(xml, str) or not xml:
            raise _deny()
        verified, _reason = validate_registered_task(xml, manifest)
        if not verified:
            raise _deny()
        raw_config = manifest.get("config_path")
        if not isinstance(raw_config, str) or not raw_config:
            raise _deny()
        source, roots = _roots(Path(raw_config))
        workdirs, _settings = load_native_config(source)
        if not workdirs or len(workdirs) != len(roots):
            raise _deny()
        return tuple(
            WorkdirClaim("default", str(root), not item.read_only)
            for item, root in zip(workdirs, roots, strict=True)
        )
    except Exception as exc:
        raise _deny() from exc


def assert_legacy_writable_roots_disjoint(manifest: Manifest) -> None:
    """Reject overlaps; allow disjoint independent Bridges with proven legacy owner.

    The already-installed v25 Bridge peers are checked separately by
    _assert_instance_isolation. Do not use this proof for the default Bridge:
    its legacy-SID endpoint remains exclusive under _legacy_bridge_active.
    """
    from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID

    from .bridge_v2_owner import load_owned_bridge_workdir_claims
    from .cli import _legacy_bridge_active

    if manifest.component != "bridge" or manifest.instance_id == DEFAULT_INSTANCE_ID:
        raise _deny()
    try:
        proposed = load_owned_bridge_workdir_claims(manifest)
        if not proposed:
            raise _deny()
        if not any(claim.writable for claim in proposed):
            return
        if not _legacy_bridge_active():
            return
        legacy_claims = verified_legacy_claims()
        assert_independent_bridge_roots(proposed, legacy_claims)
        # Fail closed if the legacy owner state changed while its policy was read.
        if not _legacy_bridge_active():
            raise _deny()
    except Exception as exc:
        raise _deny() from exc
