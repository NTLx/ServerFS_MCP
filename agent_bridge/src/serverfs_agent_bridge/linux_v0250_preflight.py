"""Read-only owner-side validation of a rendered Linux v2 Core Compose model."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .core_grants import core_workdir_policy_digest
from .core_grants_private import _parse_grants
from .errors import BridgeError
from .linux_v0250_provision import _canonical_no_links, _overlap, _read_input, _unique_pairs
from .local_ipc import max_socket_path_bytes

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_MAX_MODEL = 4 * 1024 * 1024
_BINDING = "/run/serverfs-core-bridge-binding"
_SOCKET = "/run/serverfs-agent-bridge"
_LOCKS = "/run/serverfs-agent-locks"
_SLOTS = {f"/workdirs/{slot:02d}" for slot in range(1, 17)}


class PreflightError(ValueError):
    """A fixed refusal stage, without paths, secret values or raw model data."""


def _require(condition: bool, stage: str) -> None:
    if not condition:
        raise PreflightError(stage)


def _bool(value: str, default: bool = False) -> bool:
    if not isinstance(value, str):
        raise PreflightError("boolean")
    if not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise PreflightError("boolean")


def _directory(path: Path, *, private: bool = False) -> Path:
    path = _canonical_no_links(path, must_exist=True)
    info = path.stat()
    _require(stat.S_ISDIR(info.st_mode), "directory")
    if private:
        _require(
            info.st_uid == os.getuid()
            and info.st_gid == os.getgid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "private-directory",
        )
    return path


def _private_json(path: Path) -> dict[str, Any]:
    path = _canonical_no_links(path, must_exist=True)
    info = path.stat()
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_gid == os.getgid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) in {0o400, 0o600},
        "private-file",
    )
    doc = json.loads(_read_input(path, "private JSON"), object_pairs_hook=_unique_pairs)
    _require(isinstance(doc, dict), "private-document")
    return doc


def validate(model: Any, *, bridge_id: str, core_id: str, project: str) -> None:
    """Validate one explicit owner-provisioned instance against Compose's resolved model.

    No database initialization, file writes, Docker calls or service changes.
    The operator must serialize configuration/deployment changes around this check.
    """
    _require(
        os.getuid() != 0
        and all(_ID.fullmatch(value) and value != "default" for value in (bridge_id, core_id))
        and bridge_id != core_id,
        "identity",
    )
    _require(isinstance(model, dict) and bool(project) and model.get("name") == project, "project")
    core = model["services"]["serverfs-mcp"]
    env = core["environment"]
    _require(
        isinstance(env, dict) and all(isinstance(v, str) for v in env.values()),
        "environment",
    )
    _require(core.get("user") == f"{os.getuid()}:{os.getgid()}", "container-user")
    _require(
        not core.get("privileged")
        and not core.get("cap_add")
        and not core.get("devices")
        and not core.get("volumes_from")
        and not core.get("group_add")
        and not core.get("userns_mode")
        and not core.get("network_mode")
        and not core.get("pid")
        and not core.get("ipc")
        and not core.get("configs")
        and not core.get("secrets")
        and not core.get("device_cgroup_rules")
        and all(item == "/tmp" for item in core.get("tmpfs", []))
        and core.get("read_only") is True
        and "ALL" in core.get("cap_drop", [])
        and any(
            item in {"no-new-privileges:true", "no-new-privileges=true"}
            for item in core.get("security_opt", [])
        ),
        "container-isolation",
    )
    expected_env = {
        "SERVERFS_LINUX_INSTANCE_ID": core_id,
        "SERVERFS_LINUX_BRIDGE_ID": bridge_id,
        "SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE": f"{_BINDING}/core-bridge-binding.json",
        "SERVERFS_AGENT_BRIDGE_SOCKET": f"{_SOCKET}/bridge.sock",
        "SERVERFS_AGENT_LOCK_DIR": _LOCKS,
    }
    _require(
        all(env.get(k) == v for k, v in expected_env.items())
        and _bool(env.get("SERVERFS_AGENT_BRIDGE_ENABLED", "")),
        "bridge-environment",
    )
    # Require slot-local policy; do not allow inherited global Agent enablement.
    _require(
        env.get("SERVERFS_AGENT_MODE", "disabled").strip().lower() == "disabled"
        and not env.get("SERVERFS_AGENT_RUNTIMES", "").strip(),
        "global-agent-policy",
    )
    root = _directory(
        Path.home() / ".config/serverfs-agent-bridge/instances" / bridge_id, private=True
    )
    config_path = root / "config.json"
    _private_json(config_path)
    config = BridgeConfig.load(config_path)
    _require(
        config.protocol_mode == "v2"
        and config.bridge_id == bridge_id
        and config.lease_key == "alias"
        and config.allowed_peer_uid == os.getuid()
        and config.allowed_peer_gid == os.getgid()
        and config.socket_path == root / "bridge/run/bridge.sock"
        and config.lock_dir == root / "bridge/locks"
        and config.state_dir == root / "bridge/state"
        and len(os.fsencode(config.socket_path)) <= max_socket_path_bytes(),
        "bridge-config",
    )
    _directory(config.state_dir, private=True)
    binding_dir = _directory(root / "core-bindings" / core_id, private=True)
    binding = _private_json(binding_dir / "core-bridge-binding.json")
    schema = binding.get("schema_version")
    if schema == 1:
        alias = binding.get("workdir_alias")
        _require(isinstance(alias, str) and bool(_ALIAS.fullmatch(alias)), "binding-alias")
        policy = config.policies.get(alias)
        revision = core_workdir_policy_digest(policy)
        expected_binding = {
            "schema_version": 1,
            "instance_id": core_id,
            "core_id": core_id,
            "bridge_id": bridge_id,
            "workdir_alias": alias,
            "workdir_path": "/workdirs/01",
            "policy_revision": revision,
            "socket_path": f"{_SOCKET}/bridge.sock",
        }
        _require(
            set(binding) == {*expected_binding, "key_id", "secret_hex"}
            and type(binding.get("schema_version")) is int
            and all(binding.get(k) == v for k, v in expected_binding.items()),
            "binding-policy",
        )
        selected = [{"alias": alias, "workdir_path": "/workdirs/01", "policy_revision": revision}]
    elif schema == 2 and type(schema) is int:
        _require(
            set(binding) == {
                "schema_version", "instance_id", "core_id", "bridge_id", "key_id",
                "secret_hex", "socket_path", "workdirs",
            }
            and binding["instance_id"] == core_id
            and binding["core_id"] == core_id
            and binding["bridge_id"] == bridge_id
            and binding["socket_path"] == f"{_SOCKET}/bridge.sock"
            and isinstance(binding["workdirs"], list)
            and 1 <= len(binding["workdirs"]) <= 16,
            "binding-policy",
        )
        selected = binding["workdirs"]
    else:
        raise PreflightError("binding-policy")

    scoped: dict[str, tuple[str, Any, str]] = {}
    granted_aliases: set[str] = set()
    for entry in selected:
        _require(
            isinstance(entry, dict)
            and set(entry) == {"alias", "workdir_path", "policy_revision"},
            "binding-policy",
        )
        alias, container_path, revision = (
            entry["alias"], entry["workdir_path"], entry["policy_revision"]
        )
        _require(
            isinstance(alias, str)
            and bool(_ALIAS.fullmatch(alias))
            and alias not in granted_aliases
            and isinstance(container_path, str)
            and container_path in _SLOTS
            and container_path not in scoped
            and isinstance(revision, str)
            and re.fullmatch(r"[0-9a-f]{64}", revision) is not None,
            "binding-policy",
        )
        policy = config.policies.get(alias)
        _require(
            core_workdir_policy_digest(policy) == revision
            and (policy.mode.value != "disabled" or policy.repo_ops_enabled),
            "binding-policy",
        )
        scoped[container_path] = (alias, policy, revision)
        granted_aliases.add(alias)

    grants = _parse_grants(
        _private_json(config.state_dir / "core-grants.json"), bridge_id, config.policies
    )
    matching = [
        grant for grant in grants if grant.core_id == core_id and grant.key_id == binding["key_id"]
    ]
    expected_grants = tuple(
        sorted((alias, revision) for alias, _policy, revision in scoped.values())
    )
    _require(
        len(matching) == 1
        and matching[0].workdirs == expected_grants
        and "bridge.ping" in matching[0].methods
        and isinstance(binding["secret_hex"], str)
        and hmac.compare_digest(matching[0].secret.hex(), binding["secret_hex"]),
        "grant-binding",
    )

    special = {
        _BINDING: binding_dir,
        _SOCKET: _directory(config.socket_path.parent),
        _LOCKS: _directory(config.lock_dir),
    }
    mounts = {}
    for mount in core["volumes"]:
        target = mount["target"]
        _require(
            target in _SLOTS | set(special)
            and target not in mounts
            and mount.get("type") == "bind"
            and mount.get("bind", {}).get("create_host_path") is not True,
            "mount-layout",
        )
        source = _directory(Path(mount["source"]))
        mounts[target] = (source, mount.get("read_only", False))
    _require(set(mounts) == _SLOTS | set(special), "mount-layout")
    for target, source in special.items():
        _require(mounts[target] == (source, True), "bridge-mount")
    for target in (_SOCKET, _LOCKS):
        info = special[target].stat()
        _require(
            info.st_uid == os.getuid()
            and info.st_gid == os.getgid()
            and stat.S_IMODE(info.st_mode) in {0o700, 0o750},
            "bridge-directory",
        )

    private_roots = (
        root.parent.parent,
        Path.home() / ".local/share/serverfs-agent-bridge",
        Path.home() / ".local/state/serverfs-agent-bridge",
    )
    active_roots = [policy.host_path for _, policy, _ in scoped.values()]
    aliases = {alias for alias, _, _ in scoped.values()}
    for slot in range(1, 17):
        prefix = f"WORKDIR_{slot:02d}_"
        target = f"/workdirs/{slot:02d}"
        source, readonly = mounts[target]
        _require(not any(_overlap(source, p) for p in private_roots), "private-root-exposure")
        slot_alias = env.get(prefix + "ALIAS", "").strip()
        sentinel = source / ".serverfs-disabled"
        authorized = scoped.get(target)
        if authorized is not None:
            alias, policy, _revision = authorized
            _require(
                mounts[target] == (policy.host_path, policy.read_only)
                and slot_alias == alias
                and _bool(env.get(prefix + "READ_ONLY", ""), True) == policy.read_only
                and env.get(prefix + "AGENT_MODE", "disabled").strip().lower()
                == policy.mode.value
                and _bool(env.get(prefix + "REPO_OPS_ENABLED", ""))
                == policy.repo_ops_enabled,
                "workdir-policy",
            )
            _require(
                not sentinel.exists(),
                "slot-one-disabled" if slot == 1 else "workdir-policy",
            )
            runtime_list = [
                item.strip()
                for item in env.get(prefix + "AGENT_RUNTIMES", "").split(",")
                if item.strip()
            ]
            _require(
                len(runtime_list) == len(set(runtime_list))
                and set(runtime_list) == policy.runtimes
                and (
                    (policy.mode.value == "workspace-write" and bool(policy.runtimes))
                    or (
                        policy.mode.value == "disabled"
                        and not policy.runtimes
                        and policy.repo_ops_enabled
                    )
                ),
                "runtime-policy",
            )
            continue
        _require(
            env.get(prefix + "AGENT_MODE", "").strip().lower() in {"", "disabled"}
            and not env.get(prefix + "AGENT_RUNTIMES", "").strip()
            and not _bool(env.get(prefix + "REPO_OPS_ENABLED", "")),
            "additional-agent-slot",
        )
        if not slot_alias:
            _require(
                readonly is True
                and _bool(env.get(prefix + "READ_ONLY", ""), True)
                and not _bool(env.get(prefix + "BINARY_TRANSFER_ENABLED", ""))
                and sentinel.is_file()
                and not sentinel.is_symlink()
                and {part.name for part in source.iterdir()} == {".serverfs-disabled"},
                "disabled-slot",
            )
            continue
        _require(
            bool(_ALIAS.fullmatch(slot_alias))
            and slot_alias not in aliases
            and not sentinel.exists()
            and readonly == _bool(env.get(prefix + "READ_ONLY", ""), True),
            "additional-file-slot",
        )
        _require(
            not any(_overlap(source, p) for p in active_roots)
            and not any(_overlap(source, p.host_path) for p in config.policies._by_alias.values()),
            "workdir-overlap",
        )
        active_roots.append(source)
        aliases.add(slot_alias)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-id", required=True)
    parser.add_argument("--core-id", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        _require(sys.platform == "linux", "platform")
        raw = sys.stdin.buffer.read(_MAX_MODEL + 1)
        _require(0 < len(raw) <= _MAX_MODEL, "model-size")
        model = json.loads(raw, object_pairs_hook=_unique_pairs)
        validate(model, bridge_id=args.bridge_id, core_id=args.core_id, project=args.project)
    except PreflightError as exc:
        print(f"Linux v2 preflight refused ({exc})", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        print("Linux v2 preflight refused (invalid-model-or-private-config)", file=sys.stderr)
        return 2
    except BridgeError:
        # Bridge policy errors also must not print operator-controlled text.
        print("Linux v2 preflight refused (policy-validation)", file=sys.stderr)
        return 2
    print("Linux v2 preflight passed (static owner-side configuration)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
