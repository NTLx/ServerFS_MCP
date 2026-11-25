"""Explicit, file-only initial provisioning for a Linux v2 Bridge and Cores.

This command creates a new instance tree only. It does not install or start a
unit, modify Compose projects, or rotate existing grants.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .core_grants import core_workdir_policy_digest
from .core_grants_private import provision_private_core_grants
from .local_ipc import max_socket_path_bytes
from .store import TaskStore

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_METHODS = frozenset(
    {
        "bridge.ping",
        "runtime.list",
        "runtime.models",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
        "task.submit",
        "task.list",
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "bridge_id",
        "root",
        "bridge_config",
        "expected_uid",
        "expected_gid",
        "core",
        "cores",
        "existing_writable_roots",
    }
)
_CORE_FIELDS = frozenset({"instance_id", "alias", "container_path", "host_path", "methods"})
_CORE_MULTI_FIELDS = frozenset({"instance_id", "workdirs", "methods"})
_WORKDIR_FIELDS = frozenset({"alias", "container_path", "host_path"})
_CONTAINER_SLOT = re.compile(r"/workdirs/(0[1-9]|1[0-6])\Z")
_MAX_INPUT_BYTES = 1024 * 1024


def _canonical_no_links(path: Path, *, must_exist: bool) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("provisioning paths must be absolute and canonical")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if must_exist:
                raise ValueError("required path does not exist") from None
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink path component is not allowed")
    if must_exist:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise ValueError("path must be canonical")
        return resolved
    return path


def _contained_or_equal(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _overlap(left: Path, right: Path) -> bool:
    return _contained_or_equal(left, right) or _contained_or_equal(right, left)


def _exact_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _read_input(path: Path, label: str) -> bytes:
    _canonical_no_links(path, must_exist=True)
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_INPUT_BYTES:
            raise ValueError(f"{label} must be a bounded regular file")
        raw = bytearray()
        while len(raw) <= _MAX_INPUT_BYTES:
            chunk = os.read(fd, min(65536, _MAX_INPUT_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != info.st_size or len(raw) > _MAX_INPUT_BYTES:
            raise ValueError(f"{label} changed or is too large")
        return bytes(raw)
    finally:
        os.close(fd)


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _validate(
    manifest: Any, config: BridgeConfig
) -> tuple[dict[str, Any], BridgeConfig, Path, list[dict[str, Any]]]:
    if not isinstance(manifest, dict) or set(manifest) not in (
        _MANIFEST_FIELDS - {"core", "cores"} | {"core"},
        _MANIFEST_FIELDS - {"core", "cores"} | {"cores"},
    ):
        raise ValueError("manifest fields are invalid")
    bridge_id = manifest["bridge_id"]
    cores = manifest["cores"] if "cores" in manifest else [manifest["core"]]
    if not isinstance(cores, list) or not 1 <= len(cores) <= 32:
        raise ValueError("cores must contain between 1 and 32 entries")
    if not isinstance(bridge_id, str) or not _ID.fullmatch(bridge_id) or bridge_id == "default":
        raise ValueError("bridge_id is invalid")
    core_ids: set[str] = set()
    for core in cores:
        if not isinstance(core, dict) or frozenset(core) not in {_CORE_FIELDS, _CORE_MULTI_FIELDS}:
            raise ValueError("Core entries have invalid fields")
        if "workdirs" in core and (
            not isinstance(core["workdirs"], list)
            or not 1 <= len(core["workdirs"]) <= 16
            or any(
                not isinstance(entry, dict) or set(entry) != _WORKDIR_FIELDS
                for entry in core["workdirs"]
            )
        ):
            raise ValueError("Core workdirs must be a bounded explicit array")
        core_id = core["instance_id"]
        if not isinstance(core_id, str) or not _ID.fullmatch(core_id) or core_id == "default":
            raise ValueError("instance_id is invalid")
        if core_id == bridge_id or core_id in core_ids:
            raise ValueError("Bridge and Core identities must be distinct and unique")
        core_ids.add(core_id)

    uid = _exact_int(manifest["expected_uid"], "expected_uid")
    gid = _exact_int(manifest["expected_gid"], "expected_gid")
    if uid == 0 or uid != os.getuid() or gid != os.getgid():
        raise ValueError("provisioning must run as the expected nonroot Core UID/GID")

    root = Path(manifest["root"])
    _canonical_no_links(root.parent, must_exist=True)
    if root.exists() or root.is_symlink():
        raise FileExistsError("instance root already exists")
    expected_root = Path.home() / ".config/serverfs-agent-bridge/instances" / bridge_id
    if root != expected_root:
        raise ValueError("root must match the documented per-instance user-unit config path")
    parent_info = root.parent.stat()
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise ValueError("instances parent must be owner-owned mode 0700")
    root = root.parent / root.name

    if config.protocol_mode != "v2" or config.bridge_id != bridge_id:
        raise ValueError("trusted Bridge config must use v2 and the requested bridge_id")
    if config.allowed_peer_uid != uid or config.allowed_peer_gid != gid:
        raise ValueError("Bridge peer UID/GID do not match the expected Core identity")
    if not config.policies._by_alias:
        raise ValueError("Bridge config must define workdir policies")
    if config.lease_key != "alias" or any(
        policy.slot is not None for policy in config.policies._by_alias.values()
    ):
        raise ValueError("Linux Core v2 requires alias-based Bridge leases")
    policies = config.policies._by_alias
    used_aliases: set[str] = set()
    paths_by_alias: dict[str, Path] = {}
    for core in cores:
        entries = core["workdirs"] if "workdirs" in core else [core]
        aliases_in_core: set[str] = set()
        for entry in entries:
            alias = entry["alias"]
            if not isinstance(alias, str) or alias not in policies:
                raise ValueError("Core alias does not match a trusted Bridge policy")
            if alias in aliases_in_core:
                raise ValueError("duplicate alias in one Core grant")
            aliases_in_core.add(alias)
            used_aliases.add(alias)
            policy = policies[alias]
            if policy.mode.value == "disabled" and not policy.repo_ops_enabled:
                raise ValueError("signed Core requires an Agent or Repo Ops-enabled workdir")
            host_path = _canonical_no_links(Path(entry["host_path"]), must_exist=True)
            if host_path != policy.host_path:
                raise ValueError("Core host bind source does not match the Bridge policy root")
            paths_by_alias[alias] = host_path
            container_path = entry["container_path"]
            if not isinstance(container_path, str) or not _CONTAINER_SLOT.fullmatch(container_path):
                raise ValueError("Core container_path must name one Compose workdir slot")
            if "workdirs" not in core and container_path != "/workdirs/01":
                raise ValueError("legacy single-alias Core container_path must use slot 01")
        if len({entry["container_path"] for entry in entries}) != len(entries):
            raise ValueError("duplicate Compose slot in one Core grant")
        methods = core["methods"]
        if (
            not isinstance(methods, list)
            or not methods
            or "bridge.ping" not in methods
            or any(not isinstance(method, str) or method not in _METHODS for method in methods)
            or len(methods) != len(set(methods))
        ):
            raise ValueError("methods must be an explicit least-privilege method list")
        selected_policies = [policies[entry["alias"]] for entry in entries]
        if "task.list" in methods and "task.get" not in methods:
            raise ValueError("task.list requires task.get")
        if any(method.startswith(("task.", "runtime.")) for method in methods):
            if not any(p.mode.value != "disabled" and p.runtimes for p in selected_policies):
                raise ValueError("Agent methods require an enabled Bridge runtime policy")
        if any(method.startswith("repo.") for method in methods):
            if not any(p.repo_ops_enabled for p in selected_policies):
                raise ValueError("Repo methods require repo_ops_enabled in the Bridge policy")
        if {"repo.action", "repo.commit", "repo.push"}.intersection(methods):
            if not any(p.repo_ops_enabled and not p.read_only for p in selected_policies):
                raise ValueError("repository mutation requires a writable Repo policy")
    if used_aliases != set(policies):
        raise ValueError("every Bridge policy must be used by at least one Core")

    roots = list(paths_by_alias.values())
    for index, path in enumerate(roots):
        if any(_overlap(path, other) for other in roots[index + 1 :]):
            raise ValueError("distinct workdir alias roots overlap")
    existing = manifest["existing_writable_roots"]
    if not isinstance(existing, list):
        raise ValueError("existing_writable_roots must be an explicit array")
    for raw_path in existing:
        existing_path = _canonical_no_links(Path(raw_path), must_exist=True)
        if not existing_path.is_dir():
            raise ValueError("existing_writable_roots entries must be directories")
        roots.append(existing_path)
    for index, path in enumerate(roots):
        if any(_overlap(path, other) for other in roots[index + 1 :]):
            raise ValueError("workdir roots overlap")
    instances_parent = Path.home() / ".config/serverfs-agent-bridge/instances"
    runtime_parent = Path.home() / ".local/share/serverfs-agent-bridge/instances"
    for workdir_root in roots:
        if _overlap(instances_parent, workdir_root) or _overlap(runtime_parent, workdir_root):
            raise ValueError("instance-private root overlaps a declared workdir root")

    expected_state = root / "bridge" / "state"
    expected_lock = root / "bridge" / "locks"
    expected_socket = root / "bridge" / "run" / "bridge.sock"
    if len(os.fsencode(expected_socket)) > max_socket_path_bytes():
        raise ValueError("instance socket path exceeds the Linux AF_UNIX limit")
    if (config.state_dir, config.lock_dir, config.socket_path) != (
        expected_state,
        expected_lock,
        expected_socket,
    ):
        raise ValueError("Bridge config paths must use the new instance-private root layout")
    return manifest, config, root, cores


def _publish_noreplace(stage: Path, destination: Path) -> None:
    """Atomically publish a complete new tree without replacing a racing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("atomic no-replace directory publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def provision(manifest: Any) -> Path:
    if not isinstance(manifest, dict) or set(manifest) not in (
        _MANIFEST_FIELDS - {"core", "cores"} | {"core"},
        _MANIFEST_FIELDS - {"core", "cores"} | {"cores"},
    ):
        raise ValueError("manifest fields are invalid")
    config_source = Path(manifest["bridge_config"])
    root = Path(manifest["root"])
    _canonical_no_links(root.parent, must_exist=True)
    if root.exists() or root.is_symlink():
        raise FileExistsError("instance root already exists")
    parent = root.parent
    # Validate the complete trusted config before creating an output staging tree.
    config_bytes = _read_input(config_source, "Bridge config")
    with tempfile.TemporaryDirectory(prefix=".serverfs-v0250-validate-") as validation_dir:
        config_target = Path(validation_dir) / "config.json"
        config_target.write_bytes(config_bytes)
        os.chmod(config_target, 0o600)
        raw_config = json.loads(config_bytes.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if not isinstance(raw_config, dict) or not isinstance(raw_config.get("workdirs"), list):
            raise ValueError("Bridge config workdirs are invalid")
        # BridgeConfig canonicalizes host paths; inspect raw spellings first so
        # an operator-supplied symlink cannot disappear during normalization.
        for item in raw_config["workdirs"]:
            if not isinstance(item, dict) or not isinstance(item.get("host_path"), str):
                raise ValueError("Bridge config workdir root is invalid")
            _canonical_no_links(Path(item["host_path"]), must_exist=True)
        config = BridgeConfig.load(config_target)
        manifest, config, root, cores = _validate(manifest, config)
    stage = Path(tempfile.mkdtemp(prefix=".serverfs-v0250-", dir=parent))
    try:
        os.chmod(stage, 0o700)
        config_target = stage / "config.json"
        config_target.write_bytes(config_bytes)
        os.chmod(config_target, 0o600)
        bridge_id = manifest["bridge_id"]
        identity_ids = {bridge_id, *(core["instance_id"] for core in cores)}
        bridge_dir = stage / "bridge"
        for directory in (bridge_dir / "state", bridge_dir / "locks", bridge_dir / "run"):
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
        (stage / "core-bindings").mkdir(mode=0o700)
        os.chmod(stage / "core-bindings", 0o700)
        store = TaskStore(bridge_dir / "state")
        grants = []
        credentials: list[tuple[dict[str, Any], str, bytes, list[tuple[str, str, str]]]] = []
        seen_key_ids: set[str] = set()
        seen_secrets: set[bytes] = set()
        for core in cores:
            core_id = core["instance_id"]
            entries = core["workdirs"] if "workdirs" in core else [core]
            selected_policies = [config.policies.get(entry["alias"]) for entry in entries]
            scope = [
                (
                    entry["alias"],
                    core_workdir_policy_digest(config.policies.get(entry["alias"])),
                    entry["container_path"],
                )
                for entry in entries
            ]
            key_id = "k-" + secrets.token_hex(8)
            secret = secrets.token_bytes(32)
            if key_id in identity_ids or key_id in seen_key_ids or secret in seen_secrets:
                raise ValueError("generated Core credential collision")
            seen_key_ids.add(key_id)
            seen_secrets.add(secret)
            credentials.append((core, key_id, secret, scope))
            grants.append(
                {
                    "core_id": core_id,
                    "key_id": key_id,
                    "secret_hex": secret.hex(),
                    "methods": sorted(
                        set(core["methods"])
                        | (
                            {"task.list"}
                            if "task.get" in core["methods"]
                            and any(
                                policy.mode.value != "disabled" and policy.runtimes
                                for policy in selected_policies
                            )
                            else set()
                        )
                    ),
                    "workdirs": [
                        {"alias": alias, "policy_sha256": revision} for alias, revision, _ in scope
                    ],
                }
            )
        grant_document = {"schema_version": 1, "bridge_id": bridge_id, "grants": grants}
        provision_private_core_grants(
            store=store,
            bridge_id=bridge_id,
            policies=config.policies,
            document=grant_document,
        )
        for core, key_id, secret, scope in credentials:
            core_id = core["instance_id"]
            bindings = stage / "core-bindings" / core_id
            bindings.mkdir(parents=True, mode=0o700)
            os.chmod(bindings, 0o700)
            binding = {
                "schema_version": 2 if "workdirs" in core else 1,
                "instance_id": core_id,
                "core_id": core_id,
                "bridge_id": bridge_id,
                "key_id": key_id,
                "secret_hex": secret.hex(),
                "socket_path": "/run/serverfs-agent-bridge/bridge.sock",
            }
            if "workdirs" in core:
                binding["workdirs"] = [
                    {"alias": alias, "workdir_path": container_path, "policy_revision": revision}
                    for alias, revision, container_path in scope
                ]
            else:
                alias, revision, container_path = scope[0]
                binding.update(
                    workdir_alias=alias,
                    workdir_path=container_path,
                    policy_revision=revision,
                )
            binding_file = bindings / "core-bridge-binding.json"
            with binding_file.open("x", encoding="utf-8") as stream:
                json.dump(binding, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(binding_file, 0o400)
            dir_fd = os.open(bindings, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        _publish_noreplace(stage, root)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    # Summary contains paths and IDs only. Secret material is never printed.
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="path to a private, nonsecret JSON manifest")
    args = parser.parse_args()
    try:
        if sys.platform != "linux":
            raise ValueError("Linux provisioning requires Linux")
        manifest_path = Path(args.manifest)
        document = json.loads(
            _read_input(manifest_path, "manifest").decode("utf-8"), object_pairs_hook=_unique_pairs
        )
        root = provision(document)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        print(
            "Linux v0.25 provisioning failed; check the private manifest and paths.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"provisioned": True, "root": str(root)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
