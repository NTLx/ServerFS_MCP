"""macOS v0.25: private configuration for a *named owned* Agent Bridge.

The separate Bridge distribution owns its own policy translation and private
state publication. This module is intentionally not a ServiceManagement
installer and never starts a Bridge or modifies the legacy default instance.
Input is bounded non-secret JSON on stdin, with no user-supplied socket path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from .config import BridgeConfig
from .data_home import darwin_runtime_dir
from .errors import BridgeError
from .private_state import (
    DirectoryMessages,
    ensure_private_directory,
    verify_private_file,
)
from .render_config import MAX_INPUT_BYTES, _publish_private_file, build_policy_document

_SOCKET_BUDGET = 103
_OWNER_SUFFIX = ("service-manager", "data", "agent-bridge")
_MAX_INSTANCE_LENGTH = 32
_DIRECTORY_ERRORS = DirectoryMessages(
    not_a_directory="named Bridge directory is not a directory",
    not_owned="named Bridge directory is not current-user private",
    private_mode="named Bridge directory must be private",
)


def _name(identifier: str) -> str:
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= _MAX_INSTANCE_LENGTH:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge instance ID is invalid")
    if not identifier[0].isalnum() or not identifier[-1].isalnum():
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge instance ID is invalid")
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in identifier):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge instance ID is invalid")
    if identifier == "default":
        raise BridgeError("BRIDGE_CONFIG_INVALID", "default Bridge owner is reserved")
    return identifier


def named_socket_path(identifier: str) -> Path:
    """Independently derive the same short AF_UNIX identity as the Mac Core."""
    _name(identifier)
    if sys.platform != "darwin":
        raise BridgeError("BRIDGE_PLATFORM_UNSUPPORTED", "named Bridge requires macOS")
    root = darwin_runtime_dir().resolve(strict=True)
    suffix = hashlib.sha256(identifier.encode("ascii")).hexdigest()[:16]
    socket = root / f"sfbr25-{suffix}" / "bridge.sock"
    if len(os.fsencode(str(socket))) > _SOCKET_BUDGET:
        raise BridgeError("SOCKET_PATH_TOO_LONG", "named Bridge socket exceeds Darwin budget")
    return socket


def _private_dir(path: Path) -> None:
    if path.is_symlink() or any(part.is_symlink() for part in path.parents):
        raise BridgeError("PRIVATE_STATE_UNSAFE", "named Bridge path contains a symlink")
    try:
        info = path.lstat()
    except OSError as exc:
        raise BridgeError(
            "PRIVATE_STATE_UNSAFE", "named Bridge owner directory is missing"
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("PRIVATE_STATE_UNSAFE", "named Bridge directory is not private")


def _owner_home(identifier: str, bridge_home: Path) -> None:
    _name(identifier)
    if not bridge_home.is_absolute() or bridge_home.name != "agent-bridge":
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge home is invalid")
    if (
        tuple(part.name for part in (bridge_home.parent.parent, bridge_home.parent, bridge_home))
        != _OWNER_SUFFIX
    ):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge home does not match owner")
    instance_dir = bridge_home.parent.parent.parent
    if instance_dir.name != identifier or instance_dir.parent.name != "instances":
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge home instance ID disagrees")
    for path in (instance_dir.parent, instance_dir, bridge_home.parent.parent, bridge_home.parent):
        _private_dir(path)
    if bridge_home.exists() or bridge_home.is_symlink():
        _private_dir(bridge_home)


def _config_document(
    identifier: str, bridge_home: Path, request: dict, *, allow_test_fake: bool
) -> dict:
    socket = named_socket_path(identifier)
    derived = build_policy_document(request)
    if derived.get("enable_fake_runtime") and not allow_test_fake:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "fake runtime requires isolated test mode")
    if not derived["workdirs"]:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge requires workdir policy")
    private_root = bridge_home.parent.parent
    for workdir in derived["workdirs"]:
        try:
            root = Path(workdir["host_path"]).resolve(strict=True)
        except OSError as exc:
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "named Bridge workdir cannot be resolved"
            ) from exc
        if root == private_root or root in private_root.parents or private_root in root.parents:
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID",
                "named Bridge cannot expose its private service state",
            )
    derived.update(
        {
            "socket_path": str(socket),
            "state_dir": str(bridge_home / "state"),
            "lock_dir": str(bridge_home / "locks"),
            "allowed_peer_uid": os.getuid(),
        }
    )
    # Do not pass allowed_peer_gid: it would force the Bridge socket to mode 0660
    # rather than mode 0600, weakening per-instance access control.
    return derived


def stage_named_owned_config(
    identifier: str,
    bridge_home: Path,
    request: dict,
    *,
    allow_test_fake: bool = False,
) -> dict[str, str | int]:
    """Create one NEW private named config; never replace an existing one."""
    _name(identifier)
    if sys.platform != "darwin":
        raise BridgeError("BRIDGE_PLATFORM_UNSUPPORTED", "named Bridge requires macOS")
    bridge_home = Path(bridge_home)
    _owner_home(identifier, bridge_home)
    config_path = bridge_home / "bridge.json"
    if config_path.exists() or config_path.is_symlink():
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge config already exists")
    socket = named_socket_path(identifier)
    if socket.parent.exists() or socket.parent.is_symlink():
        raise BridgeError("SOCKET_PATH_IN_USE", "named Bridge socket namespace is occupied")
    document = _config_document(identifier, bridge_home, request, allow_test_fake=allow_test_fake)
    created: list[Path] = []
    published = False
    try:
        for directory in (bridge_home, bridge_home / "state", bridge_home / "locks"):
            if directory.exists() or directory.is_symlink():
                _private_dir(directory)
            else:
                ensure_private_directory(directory, mode=0o700, messages=_DIRECTORY_ERRORS)
                created.append(directory)
        _publish_private_file(config_path, json.dumps(document, sort_keys=True) + "\n")
        published = True
        config = BridgeConfig.load(config_path)
        if (
            config.socket_path != Path(document["socket_path"])
            or config.state_dir != bridge_home / "state"
            or config.lock_dir != bridge_home / "locks"
            or config.allowed_peer_uid != os.getuid()
            or config.allowed_peer_gid is not None
        ):
            raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge identity mismatch")
        verify_private_file(
            config_path,
            not_regular="named Bridge config is not regular",
            not_private="named Bridge config is not private",
        )
    except Exception:
        # Only the newly published config and empty directories created here
        # may be removed; never delete pre-existing Bridge data.
        if published and config_path.is_file() and not config_path.is_symlink():
            config_path.unlink()
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return {
        "instance_id": identifier,
        "state": "staged-not-registered",
        "private_config": "verified",
        "workdirs": len(config.policies.lease_ids()),
    }


def stage_named_owned_v2_config(
    identifier: str,
    bridge_home: Path,
    request: dict,
    grant_materials: list[dict[str, str]],
    *,
    allow_test_fake: bool = False,
) -> dict[str, object]:
    """Create a new self-owned v2 Bridge and one private CoreGrant per Agent alias.

    This is a staging primitive only. It does not register or start the Bridge,
    alter an existing v1 owner, or provision authority for another Bridge.
    """
    _name(identifier)
    if sys.platform != "darwin":
        raise BridgeError("BRIDGE_PLATFORM_UNSUPPORTED", "named Bridge requires macOS")
    from . import core_grants
    from .core_grants import core_workdir_policy_digest

    if not isinstance(grant_materials, list) or not 1 <= len(grant_materials) <= 16:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "owned Core grant material is invalid")
    materials: dict[str, tuple[str, str]] = {}
    for item in grant_materials:
        if not isinstance(item, dict) or set(item) != {"alias", "key_id", "secret_hex"}:
            raise BridgeError("BRIDGE_CONFIG_INVALID", "owned Core grant material is invalid")
        alias, key_id, secret_hex = item["alias"], item["key_id"], item["secret_hex"]
        if (
            not isinstance(alias, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", alias) is None
            or not isinstance(key_id, str)
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", key_id) is None
            or key_id == "default"
            or not isinstance(secret_hex, str)
            or re.fullmatch(r"[0-9a-f]{64}", secret_hex) is None
            or alias in materials
            or any(
                key_id == existing[0] or secret_hex == existing[1]
                for existing in materials.values()
            )
        ):
            raise BridgeError("BRIDGE_CONFIG_INVALID", "owned Core grant material is invalid")
        materials[alias] = (key_id, secret_hex)

    bridge_home = Path(bridge_home)
    _owner_home(identifier, bridge_home)
    config_path = bridge_home / "bridge.json"
    grants_path = bridge_home / "state" / "core-grants.json"
    if config_path.exists() or config_path.is_symlink():
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge config already exists")
    if grants_path.exists() or grants_path.is_symlink():
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge grants already exist")
    methods = frozenset(
        method
        for method in core_grants._WORKDIR_METHODS
        if method == "bridge.ping" or method.startswith(("runtime.", "task."))
    )
    if "task.list" not in methods:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "Bridge task.list capability is unavailable")
    socket = named_socket_path(identifier)
    if socket.parent.exists() or socket.parent.is_symlink():
        raise BridgeError("SOCKET_PATH_IN_USE", "named Bridge socket namespace is occupied")

    document = _config_document(identifier, bridge_home, request, allow_test_fake=allow_test_fake)
    document["protocol_mode"] = "v2"
    document["bridge_id"] = identifier
    created: list[Path] = []
    published = False
    try:
        for directory in (bridge_home, bridge_home / "state", bridge_home / "locks"):
            if directory.exists() or directory.is_symlink():
                _private_dir(directory)
            else:
                ensure_private_directory(directory, mode=0o700, messages=_DIRECTORY_ERRORS)
                created.append(directory)
        _publish_private_file(config_path, json.dumps(document, sort_keys=True) + "\n")
        published = True
        config = BridgeConfig.load(config_path)
        scoped_policies = {
            alias: policy
            for alias, policy in config.policies._by_alias.items()
            if policy.mode.value != "disabled" or policy.repo_ops_enabled
        }
        if not scoped_policies or set(materials) != set(scoped_policies):
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "owned Core grants do not match Bridge-enabled aliases"
            )
        grant_scopes: list[dict[str, str]] = []
        grants: list[dict[str, object]] = []
        for alias, policy in scoped_policies.items():
            allowed = {"bridge.ping"}
            if policy.mode.value != "disabled" and policy.runtimes:
                allowed.update(methods)
            if policy.repo_ops_enabled:
                allowed.add("repo.inspect")
                if not policy.read_only:
                    allowed.update({"repo.action", "repo.commit", "repo.push"})
            if not allowed <= core_grants._WORKDIR_METHODS or len(allowed) == 1:
                raise BridgeError("BRIDGE_CONFIG_INVALID", "owned Core grant scope is invalid")
            key_id, secret_hex = materials[alias]
            revision = core_workdir_policy_digest(policy)
            grant_scopes.append({"alias": alias, "key_id": key_id, "policy_revision": revision})
            grants.append(
                {
                    "core_id": identifier,
                    "key_id": key_id,
                    "secret_hex": secret_hex,
                    "methods": sorted(allowed),
                    "workdirs": [{"alias": alias, "policy_sha256": revision}],
                }
            )

        from .core_grants_private import provision_private_core_grants
        from .store import TaskStore

        store = TaskStore(config.state_dir)
        provision_private_core_grants(
            store=store,
            bridge_id=identifier,
            policies=config.policies,
            document={"schema_version": 1, "bridge_id": identifier, "grants": grants},
        )
        verify_private_file(
            grants_path,
            not_regular="owned Bridge grants are not regular",
            not_private="owned Bridge grants are not private",
        )
        verify_private_file(
            config_path,
            not_regular="named Bridge config is not regular",
            not_private="named Bridge config is not private",
        )
    except Exception:
        # Preserve any private grant/database state created by the owner helper;
        # only remove this invocation's newly published config and empty dirs.
        if published and config_path.is_file() and not config_path.is_symlink():
            config_path.unlink()
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return {
        "instance_id": identifier,
        "state": "staged-not-registered",
        "private_config": "verified",
        "protocol_mode": "v2",
        "grant_scopes": grant_scopes,
    }


def check_named_owned_config(
    identifier: str,
    bridge_home: Path,
    request: dict,
    *,
    allow_test_fake: bool = False,
) -> dict[str, str | int]:
    """Read-only verification of both owner identity and exact derived policy."""
    _name(identifier)
    if sys.platform != "darwin":
        raise BridgeError("BRIDGE_PLATFORM_UNSUPPORTED", "named Bridge requires macOS")
    bridge_home = Path(bridge_home)
    _owner_home(identifier, bridge_home)
    _private_dir(bridge_home)
    for directory in (bridge_home / "state", bridge_home / "locks"):
        _private_dir(directory)
    path = bridge_home / "bridge.json"
    verify_private_file(
        path,
        not_regular="named Bridge config is not regular",
        not_private="named Bridge config is not private",
    )
    derived = _config_document(identifier, bridge_home, request, allow_test_fake=allow_test_fake)
    actual = BridgeConfig.load(path)
    expected_bridge_id = identifier if actual.protocol_mode == "v2" else None
    if (
        actual.protocol_mode not in {"v1", "v2"}
        or actual.bridge_id != expected_bridge_id
        or actual.socket_path != Path(derived["socket_path"])
        or actual.state_dir != Path(derived["state_dir"])
        or actual.lock_dir != Path(derived["lock_dir"])
        or actual.allowed_peer_uid != os.getuid()
        or actual.allowed_peer_gid is not None
    ):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge runtime identity drift")
    if actual.protocol_mode == "v2":
        from .core_grants import _WORKDIR_METHODS

        if "task.list" not in _WORKDIR_METHODS:
            raise BridgeError("BRIDGE_CONFIG_INVALID", "Bridge task.list capability is unavailable")
    from .sync_config import _policy_matches

    # _policy_matches operates on only operator-owned non-secret policy fields.
    # Do not return/log existing private proxy/Jev blocks.
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge policy is unreadable") from exc
    if not _policy_matches(document, request):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge TOML policy drift")
    return {
        "instance_id": identifier,
        "state": "verified-not-registered",
        "policy": "synced",
        "protocol_mode": actual.protocol_mode,
        "workdirs": len(actual.policies.lease_ids()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serverfs-agent-bridge-macos-owned-config")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify an already staged config without modifying private state",
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("named Bridge requires macOS", file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        print("named Bridge request is too large", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise BridgeError("BRIDGE_CONFIG_INVALID", "named Bridge request must be an object")
        if args.check:
            result = check_named_owned_config(args.instance, args.bridge_home, request)
        elif set(request) == {"policy", "grant_materials"}:
            policy = request["policy"]
            if not isinstance(policy, dict) or not isinstance(request["grant_materials"], list):
                raise BridgeError("BRIDGE_CONFIG_INVALID", "owned v2 staging request is invalid")
            result = stage_named_owned_v2_config(
                args.instance, args.bridge_home, policy, request["grant_materials"]
            )
        else:
            result = stage_named_owned_config(args.instance, args.bridge_home, request)
    except (BridgeError, ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"named Bridge config refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
