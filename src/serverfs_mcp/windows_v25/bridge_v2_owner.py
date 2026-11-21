"""Windows v0.25 independently owned Bridge v2: private plan and identity gates.

The owner does not render a legacy SID-global Bridge config or reuse its
lifecycle lease. This module only reads operator-provisioned, private files.
It never installs, starts, stops or mutates a Task Scheduler entry.
"""

from __future__ import annotations

import asyncio
import json
import ntpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from serverfs_mcp.agent_client import AgentBridgeClientError, AgentBridgeUnavailable
from serverfs_mcp.agent_core_grant_client import CoreGrantBridgeClient
from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, valid_id

from . import storage
from .bridge_resources import WindowsBridgeResources
from .spec import Manifest
from .workdir_ownership import WorkdirClaim

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


class BridgeV2OwnerError(ValueError):
    """Redacted independent Bridge owner preflight refusal."""


def _key(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise BridgeV2OwnerError("independent Bridge resource is invalid")
    return ntpath.normcase(ntpath.normpath(str(value)))


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise BridgeV2OwnerError("independent Bridge JSON is ambiguous")
        result[key] = value
    return result


@dataclass(frozen=True)
class NativeBridgeV2Plan:
    bridge_id: str
    config_path: Path
    bridge_python: Path
    state_dir: Path
    lock_dir: Path
    pipe_name: str
    job_name: str
    lease_artifact: str
    health_core_id: str
    health_key_id: str
    health_workdir: str
    health_policy_digest: str
    health_secret: bytes = field(repr=False)


def inspect_native_bridge_contract(
    manifest: Manifest,
    config: object,
    grants: object,
    *,
    owner_home: Path,
    worker_python: Path,
) -> NativeBridgeV2Plan:
    """Compare two untrusted JSON documents against one owned OS identity.

    No arbitrary path, SID, peer ID or executable in these documents is
    trusted. This pure contract is testable off Windows.
    """
    if (
        manifest.component != "bridge"
        or manifest.schema != 2
        or manifest.instance_id == DEFAULT_INSTANCE_ID
    ):
        raise BridgeV2OwnerError("independent Bridge requires a v2 service manifest")
    valid_id(manifest.instance_id)
    resources = WindowsBridgeResources(manifest.sid, manifest.instance_id)
    expected_config = owner_home / "bridge.json"
    expected_state = owner_home / "state"
    expected_locks = owner_home / "locks"
    if _key(manifest.settings["config"]) != _key(expected_config) or _key(
        manifest.settings["bridge_python"]
    ) != _key(worker_python):
        raise BridgeV2OwnerError("independent Bridge inputs must use the owned installation")
    if not isinstance(config, dict) or not isinstance(grants, dict):
        raise BridgeV2OwnerError("independent Bridge documents are invalid")
    if (
        config.get("bridge_id") != manifest.instance_id
        or config.get("core_grants_enabled") is not True
        or _key(config.get("socket_path")) != _key(resources.pipe_name)
        or _key(config.get("state_dir")) != _key(expected_state)
        or _key(config.get("lock_dir")) != _key(expected_locks)
        or config.get("allowed_peer_sid") != manifest.sid
    ):
        raise BridgeV2OwnerError("independent Bridge configuration does not match its owner")
    if (
        grants.get("schema_version") != 1
        or type(grants["schema_version"]) is not int
        or grants.get("bridge_id") != manifest.instance_id
        or not isinstance(grants.get("grants"), list)
    ):
        raise BridgeV2OwnerError("independent Bridge grants are invalid")
    raw_workdirs = config.get("workdirs")
    if not isinstance(raw_workdirs, list):
        raise BridgeV2OwnerError("independent Bridge workdir configuration is invalid")
    aliases = {
        item["alias"]
        for item in raw_workdirs
        if isinstance(item, dict) and isinstance(item.get("alias"), str)
    }
    health_candidates: list[tuple[str, str, str, str, bytes]] = []
    for raw in grants["grants"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("methods"), list):
            raise BridgeV2OwnerError("independent Bridge grants are invalid")
        if "bridge.ping" not in raw["methods"]:
            continue
        try:
            core_id = valid_id(raw["core_id"])
            key_id = valid_id(raw["key_id"])
            secret = raw["secret_hex"]
            if (
                not isinstance(secret, str)
                or not _HEX64.fullmatch(secret)
                or not isinstance(raw["workdirs"], list)
            ):
                raise ValueError("invalid health capability")
            for workdir in raw["workdirs"]:
                alias = workdir["alias"]
                digest = workdir["policy_sha256"]
                if (
                    alias in aliases
                    and isinstance(alias, str)
                    and _ALIAS.fullmatch(alias)
                    and isinstance(digest, str)
                    and _HEX64.fullmatch(digest)
                ):
                    health_candidates.append(
                        (core_id, key_id, alias, digest, bytes.fromhex(secret))
                    )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise BridgeV2OwnerError("independent Bridge health grant is invalid") from exc
    if not health_candidates:
        raise BridgeV2OwnerError("independent Bridge requires a signed health capability")
    # Sort by nonsecret identifiers; neither secrets nor arbitrary paths are logged.
    core_id, key_id, alias, digest, secret = sorted(health_candidates, key=lambda entry: entry[:4])[
        0
    ]
    return NativeBridgeV2Plan(
        bridge_id=manifest.instance_id,
        config_path=expected_config,
        bridge_python=worker_python,
        state_dir=expected_state,
        lock_dir=expected_locks,
        pipe_name=resources.pipe_name,
        job_name=resources.job_name,
        lease_artifact=resources.lifecycle_artifact_name,
        health_core_id=core_id,
        health_key_id=key_id,
        health_workdir=alias,
        health_policy_digest=digest,
        health_secret=secret,
    )


def load_native_bridge_plan(manifest: Manifest) -> NativeBridgeV2Plan:
    """Fail closed on absent, redirected, foreign or malformed private state."""
    if manifest.component != "bridge" or manifest.instance_id == DEFAULT_INSTANCE_ID:
        raise BridgeV2OwnerError("nondefault Bridge plan requires a separate owner")
    base = storage.root(manifest.instance_id)
    home = storage.home("bridge", manifest.instance_id)
    worker = base / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    state = home / "state"
    locks = home / "locks"
    config_path = home / "bridge.json"
    grants_path = state / "core-grants.json"
    try:
        for path in (home, state, locks):
            storage.check_private(path, directory=True, sid=manifest.sid)
        for path in (worker, config_path, grants_path):
            storage.check_private(path, directory=False, sid=manifest.sid)
        if config_path.stat().st_size > 256 * 1024 or grants_path.stat().st_size > 128 * 1024:
            raise ValueError("oversized protected Bridge document")
        config = json.loads(config_path.read_text("utf-8"), object_pairs_hook=_unique_pairs)
        grants = json.loads(grants_path.read_text("utf-8"), object_pairs_hook=_unique_pairs)
        return inspect_native_bridge_contract(
            manifest, config, grants, owner_home=home, worker_python=worker
        )
    except Exception as exc:
        raise BridgeV2OwnerError("independent Bridge private owner plan is unverified") from exc


def bridge_workdir_claims_from_document(
    bridge_id: str,
    config: object,
    *,
    canonicalize,
) -> tuple[WorkdirClaim, ...]:
    """Project Bridge policy into cross-owner writable-root claims.

    Pure caller-injected canonicalization makes the OS collision rule
    independently testable without bypassing production path checks.
    """
    valid_id(bridge_id)
    if not isinstance(config, dict):
        raise BridgeV2OwnerError("independent Bridge workdir policy is invalid")
    entries = config.get("workdirs")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise BridgeV2OwnerError("independent Bridge workdir policy is invalid")
    claims = []
    for item in entries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("host_path"), str)
            or type(item.get("read_only", True)) is not bool
        ):
            raise BridgeV2OwnerError("independent Bridge workdir ownership is invalid")
        try:
            root = canonicalize(item["host_path"])
            claims.append(WorkdirClaim(bridge_id, root, not item.get("read_only", True)))
        except (OSError, ValueError, TypeError) as exc:
            raise BridgeV2OwnerError("independent Bridge workdir ownership is invalid") from exc
    return tuple(claims)


def load_owned_bridge_workdir_claims(manifest: Manifest) -> tuple[WorkdirClaim, ...]:
    """Private manifests and canonical OS paths only; no installed task changes."""
    if manifest.component != "bridge":
        raise BridgeV2OwnerError("Bridge ownership inventory needs a Bridge")
    if manifest.instance_id == DEFAULT_INSTANCE_ID:
        from serverfs_mcp.native_config import load_native_config

        from .runtime import _roots

        # The legacy/default Bridge shares the Core's native TOML policy,
        # which may live in the checkout. Its startup contract validates
        # canonical paths/reparse points, not a v25 private-file DACL.
        # Keep the strict private DACL for every nondefault Bridge below.
        config, canonical_roots = _roots(Path(str(manifest.settings["config"])))
        workdirs, _settings = load_native_config(config)
        if len(canonical_roots) != len(workdirs) or any(
            item.root.resolve(strict=True) != root
            for item, root in zip(workdirs, canonical_roots, strict=True)
        ):
            raise BridgeV2OwnerError("default Bridge workdir inventory is inconsistent")
        return tuple(
            WorkdirClaim("default", str(root), not item.read_only)
            for item, root in zip(workdirs, canonical_roots, strict=True)
        )

    plan = load_native_bridge_plan(manifest)
    try:
        raw = json.loads(plan.config_path.read_text("utf-8"), object_pairs_hook=_unique_pairs)

        def canonicalize(value: str) -> str:
            from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

            original = Path(value)
            if not original.is_absolute():
                raise ValueError("Bridge root must be absolute")
            _no_reparse_components(original)
            result = original.resolve(strict=True)
            if not result.is_dir():
                raise ValueError("Bridge root is not a directory")
            return str(result)

        return bridge_workdir_claims_from_document(plan.bridge_id, raw, canonicalize=canonicalize)
    except Exception as exc:
        raise BridgeV2OwnerError("independent Bridge workdir inventory is unverified") from exc


async def wait_for_signed_bridge_v2_ready(
    plan: NativeBridgeV2Plan,
    process: object,
    *,
    server_pid_validator,
    timeout: float = 15.0,
) -> None:
    """Only a signed, policy-bound v2 ping from the held Job counts as readiness.

    The same-SID pipe alone is not identity proof. The caller must supply the
    current kernel Job membership verifier, not an unverified PID-file match.
    A protocol-level refusal is fatal rather than retried indefinitely.
    """
    if not callable(server_pid_validator):
        raise BridgeV2OwnerError("Bridge owner-generation proof is unavailable")
    client = CoreGrantBridgeClient(
        Path(plan.pipe_name),
        bridge_id=plan.bridge_id,
        core_id=plan.health_core_id,
        key_id=plan.health_key_id,
        secret=plan.health_secret,
        policy_revisions={plan.health_workdir: plan.health_policy_digest},
        timeout_seconds=min(timeout, 3.0),
        server_pid_validator=server_pid_validator,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if process.poll() is not None:
            raise BridgeV2OwnerError("independent Bridge exited before authenticated readiness")
        try:
            response = await client.call("bridge.ping", {"workdir": plan.health_workdir})
            if response != {"status": "ok"}:
                raise BridgeV2OwnerError("independent Bridge health response is invalid")
            return
        except AgentBridgeUnavailable:
            await asyncio.sleep(0.2)
        except AgentBridgeClientError as exc:
            raise BridgeV2OwnerError("independent Bridge signed readiness was denied") from exc
    raise BridgeV2OwnerError("independent Bridge signed readiness timed out")
