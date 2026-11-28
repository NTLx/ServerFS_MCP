"""Opt-in v25 Windows Core -> owned/shared Bridge protocol-v2 binding.

Each Core has one private, operator-provisioned HMAC identity. The binding
contains NO endpoint, Job name or filesystem root supplied by the client;
those derive from the verified current-user Bridge manifest and bridge_id.
Core startup does not start, stop, adopt or supervise the referenced Bridge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity, valid_id

from . import storage
from .bridge_resources import WindowsBridgeResources
from .spec import Manifest

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_MAX_FILE_BYTES = 32 * 1024
_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "instance_id",
        "bridge_mode",
        "bridge_id",
        "key_id",
        "secret_hex",
        "workdirs",
    }
)
_WORKDIR_FIELDS = frozenset({"alias", "policy_sha256"})


class CoreBridgeBindingError(ValueError):
    """No trusted, nonsecret native Core-v2 Bridge binding is available."""


def _deny() -> CoreBridgeBindingError:
    return CoreBridgeBindingError("independent Core Bridge binding is invalid or not private")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise _deny()
        value[name] = item
    return value


@dataclass(frozen=True)
class NativeCoreBridgeBinding:
    instance_id: str
    bridge_mode: str
    bridge_id: str
    key_id: str
    secret: bytes = field(repr=False)
    policy_revisions: tuple[tuple[str, str], ...]
    pipe_name: str
    job_name: str
    lock_dir: Path

    def build_wiring(self, *, log_level: str = "INFO", peer_validator=None):
        """No ambient env variables, no fallback to v1; Core owns only its client."""
        from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
        from serverfs_mcp.config import Settings

        if peer_validator is None:
            from .bridge_peer_job import make_bridge_peer_validator
            from .storage import _sid

            peer_validator = make_bridge_peer_validator(bridge_id=self.bridge_id, user_sid=_sid())
        settings = Settings(
            log_level=log_level,
            agent_bridge_enabled=True,
            agent_bridge_socket=self.pipe_name,
            agent_lock_dir=str(self.lock_dir),
        )
        client = CoreGrantBridgeClient(
            Path(self.pipe_name),
            core_id=self.instance_id,
            bridge_id=self.bridge_id,
            key_id=self.key_id,
            secret=self.secret,
            policy_revisions=dict(self.policy_revisions),
            timeout_seconds=30.0,
            server_pid_validator=peer_validator,
        )
        return settings, client


def parse_core_bridge_binding(
    manifest: Manifest,
    document: object,
    *,
    local_aliases: frozenset[str],
    verified_bridge: Manifest,
    bridge_lock_dir: Path,
) -> NativeCoreBridgeBinding:
    """Pure authenticated plan: exact Core ID, Bridge ID, aliases and OS resources."""
    try:
        if (
            manifest.component != "core"
            or manifest.instance_id == DEFAULT_INSTANCE_ID
            or manifest.schema != 2
            or not isinstance(document, dict)
            or set(document) != _TOP_FIELDS
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["instance_id"] != manifest.instance_id
        ):
            raise _deny()
        bridge_mode = document["bridge_mode"]
        bridge_id = document["bridge_id"]
        InstanceIdentity(manifest.instance_id, bridge_mode, bridge_id)
        if bridge_mode not in ("shared", "owned") or bridge_id == DEFAULT_INSTANCE_ID:
            raise _deny()
        if (
            verified_bridge.component != "bridge"
            or verified_bridge.instance_id != bridge_id
            or verified_bridge.schema != 2
            or verified_bridge.sid != manifest.sid
        ):
            raise _deny()
        key_id = valid_id(document["key_id"])
        secret_hex = document["secret_hex"]
        if not isinstance(secret_hex, str) or not _HEX.fullmatch(secret_hex):
            raise _deny()
        workdirs = document["workdirs"]
        if not isinstance(workdirs, list) or not 1 <= len(workdirs) <= 16:
            raise _deny()
        pinned: dict[str, str] = {}
        for item in workdirs:
            if not isinstance(item, dict) or set(item) != _WORKDIR_FIELDS:
                raise _deny()
            alias = item["alias"]
            digest = item["policy_sha256"]
            if (
                not isinstance(alias, str)
                or not _ALIAS.fullmatch(alias)
                or alias in pinned
                or not isinstance(digest, str)
                or not _HEX.fullmatch(digest)
            ):
                raise _deny()
            pinned[alias] = digest
        if set(pinned) != local_aliases or not pinned:
            raise _deny()
        resources = WindowsBridgeResources(manifest.sid, bridge_id)
        return NativeCoreBridgeBinding(
            instance_id=manifest.instance_id,
            bridge_mode=bridge_mode,
            bridge_id=bridge_id,
            key_id=key_id,
            secret=bytes.fromhex(secret_hex),
            policy_revisions=tuple(sorted(pinned.items())),
            pipe_name=resources.pipe_name,
            job_name=resources.job_name,
            lock_dir=bridge_lock_dir,
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        raise _deny() from None


def verify_core_peer_bridge_owner(
    manifest: Manifest, bridge_config: object, *, owner_home: Path
) -> None:
    """Core validates one Bridge's nonsecret owner contract, not its grants."""
    import ntpath

    if (
        manifest.component != "bridge"
        or manifest.schema != 2
        or manifest.instance_id == DEFAULT_INSTANCE_ID
        or not isinstance(bridge_config, dict)
    ):
        raise _deny()
    resources = WindowsBridgeResources(manifest.sid, manifest.instance_id)

    def same_path(raw: object, owned: Path | str) -> bool:
        return isinstance(raw, str) and ntpath.normcase(ntpath.normpath(raw)) == (
            ntpath.normcase(ntpath.normpath(str(owned)))
        )

    if (
        bridge_config.get("bridge_id") != manifest.instance_id
        or bridge_config.get("core_grants_enabled") is not True
        or not same_path(bridge_config.get("socket_path"), resources.pipe_name)
        or not same_path(bridge_config.get("state_dir"), owner_home / "state")
        or not same_path(bridge_config.get("lock_dir"), owner_home / "locks")
        or bridge_config.get("allowed_peer_sid") != manifest.sid
    ):
        raise _deny()


def verify_core_bridge_workdirs(
    workdirs: list,
    bridge_config: object,
    *,
    canonicalize,
) -> None:
    """Reject Core/Bridge scope drift before a bound Core can start.

    A valid HMAC/policy digest alone only proves which *Bridge* workdir was
    authorized.  If Core's local alias points to another host root, existing
    public file tools and Agent/Repo Ops would operate on different trees.
    Resolve both roots with the host's real filesystem (including reparse
    rejection) and require matching effective access/mode/runtime gates.

    Other Bridge aliases may be granted to independent Cores; this Core only
    needs exact parity for its Agent/Repo-enabled aliases.
    """
    import ntpath

    if not isinstance(bridge_config, dict):
        raise _deny()
    entries = bridge_config.get("workdirs")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64 or not callable(canonicalize):
        raise _deny()
    by_alias: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise _deny()
        alias = entry.get("alias")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in by_alias:
            raise _deny()
        by_alias[alias] = entry

    selected = [w for w in workdirs if w.agent_mode != "disabled" or w.policy.repo_ops_enabled]
    if not selected or len(selected) > 16:
        raise _deny()
    observed: set[str] = set()
    try:
        for core in selected:
            alias = core.alias
            if alias in observed or alias not in by_alias:
                raise _deny()
            observed.add(alias)
            peer = by_alias[alias]
            runtimes = peer.get("agent_runtimes", [])
            if (
                type(peer.get("read_only", True)) is not bool
                or type(peer.get("repo_ops_enabled", False)) is not bool
                or not isinstance(runtimes, list)
                or any(not isinstance(runtime, str) for runtime in runtimes)
                or len(set(runtimes)) != len(runtimes)
                or peer.get("read_only", True) != core.read_only
                or peer.get("agent_mode", "disabled") != core.agent_mode
                or frozenset(runtimes) != core.agent_runtimes
                or peer.get("repo_ops_enabled", False) != core.policy.repo_ops_enabled
            ):
                raise _deny()
            peer_root = peer.get("host_path")
            if not isinstance(peer_root, str) or not peer_root:
                raise _deny()
            actual = ntpath.normcase(ntpath.normpath(str(canonicalize(peer_root))))
            expected = ntpath.normcase(ntpath.normpath(str(canonicalize(str(core.root)))))
            if actual != expected:
                raise _deny()
    except (OSError, TypeError, ValueError, AttributeError, KeyError):
        raise _deny() from None


def load_native_core_bridge_binding(manifest: Manifest, workdirs: list) -> NativeCoreBridgeBinding:
    """Owner-private Core binding file; verify the separate Bridge manifest."""
    if manifest.component != "core" or manifest.instance_id == DEFAULT_INSTANCE_ID:
        raise _deny()
    path = storage.home("core", manifest.instance_id) / "bridge-binding.json"
    try:
        storage.check_private(path, directory=False, sid=manifest.sid)
        if not 2 <= path.stat().st_size <= _MAX_FILE_BYTES:
            raise ValueError("Core binding file size invalid")
        document = json.loads(path.read_text("utf-8"), object_pairs_hook=_unique_pairs)
        if not isinstance(document, dict):
            raise _deny()
        requested = valid_id(document.get("bridge_id"))
        if requested == DEFAULT_INSTANCE_ID:
            raise _deny()
        bridge = storage.load("bridge", requested)
        from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

        # Core checks peer policy but never loads the Bridge owner's private
        # signed-health HMAC key/grant file: independent owners retain secrets.
        home = storage.home("bridge", requested)
        bridge_config = home / "bridge.json"
        storage.check_private(bridge_config, directory=False, sid=manifest.sid)
        if not 2 <= bridge_config.stat().st_size <= 256 * 1024:
            raise _deny()
        bridge_document = json.loads(
            bridge_config.read_text("utf-8"), object_pairs_hook=_unique_pairs
        )
        verify_core_peer_bridge_owner(bridge, bridge_document, owner_home=home)

        def canonicalize(raw: str) -> Path:
            candidate = Path(raw)
            if not candidate.is_absolute():
                raise _deny()
            _no_reparse_components(candidate)
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir():
                raise _deny()
            return resolved

        verify_core_bridge_workdirs(workdirs, bridge_document, canonicalize=canonicalize)
        lock_dir = home / "locks"
        storage.check_private(lock_dir, directory=True, sid=manifest.sid)
        aliases = frozenset(
            item.alias
            for item in workdirs
            if item.agent_mode != "disabled" or item.policy.repo_ops_enabled
        )
        return parse_core_bridge_binding(
            manifest,
            document,
            local_aliases=aliases,
            verified_bridge=bridge,
            bridge_lock_dir=lock_dir,
        )
    except Exception:
        raise _deny() from None
