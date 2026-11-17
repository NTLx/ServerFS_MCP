"""macOS v0.25 *planning* for owned Bridge resource isolation.

Do not wire this directly into live Agent Bridge until the Core-side endpoint
selection and same-user authorization contract is reconciled across platforms.
These are deterministic, independently owned resource names; no OS mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .darwin_instances import instance_service_home
from .instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentity

if TYPE_CHECKING:
    from .workdirs import Workdir

# Native Darwin sockaddr_un sun_path budget is 104 bytes, including the NUL.
# The existing implementation checks its own platform-specific constant.
_DARWIN_SOCKET_BYTES = 103


class DarwinBridgeIdentityError(ValueError):
    """An instance cannot claim an unsafe or shared Bridge owner."""


@dataclass(frozen=True)
class NamedBridgeResources:
    bridge_id: str
    socket_path: Path
    state_dir: Path
    lock_dir: Path
    config_path: Path


def named_owned_bridge_resources(
    identity: InstanceIdentity,
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
) -> NamedBridgeResources:
    """Return private, non-default Bridge resources; do not create or start them."""
    if (
        identity.instance_id == DEFAULT_INSTANCE_ID
        or identity.bridge_mode != "owned"
        or identity.bridge_id != identity.instance_id
    ):
        raise DarwinBridgeIdentityError(
            "non-default owned Bridge is required for a private resource plan"
        )
    if runtime_root is None:
        # The OS-provided per-user runtime directory (confstr), not $TMPDIR.
        from .darwin_libc import darwin_runtime_dir

        # confstr supplies an OS-owned per-user directory that may be
        # spelled through the platform's /var -> /private/var symlink.
        # Canonicalize only this verified OS source; explicit caller paths
        # continue to reject symlinks in their parent chain.
        runtime_root = darwin_runtime_dir().resolve(strict=True)
    runtime_root = Path(runtime_root)
    if not runtime_root.is_absolute() or runtime_root.is_symlink():
        raise DarwinBridgeIdentityError("Darwin runtime root must be absolute and not a symlink")
    if any(path.is_symlink() for path in runtime_root.parents):
        raise DarwinBridgeIdentityError("Darwin runtime path includes a symlink")
    # Keep sun_path shorter than the legacy socket even for a 32-char Bridge ID.
    # Full bridge_id remains in the private state; collision checks compare paths.
    suffix = hashlib.sha256(identity.bridge_id.encode("ascii")).hexdigest()[:16]
    socket_path = runtime_root / f"sfbr25-{suffix}" / "bridge.sock"
    if len(os.fsencode(str(socket_path))) > _DARWIN_SOCKET_BYTES:
        raise DarwinBridgeIdentityError("named Bridge socket exceeds Darwin sun_path budget")

    service_root = instance_service_home(identity, home=home)
    state = service_root / "data" / "agent-bridge"
    return NamedBridgeResources(
        bridge_id=identity.bridge_id,
        socket_path=socket_path,
        state_dir=state / "state",
        lock_dir=state / "locks",
        config_path=state / "bridge.json",
    )


def refuse_writable_workdir_overlaps(
    proposed: tuple[Workdir, ...],
    other_bridges: tuple[tuple[Workdir, ...], ...],
) -> None:
    """Reject independent Bridge writer claims sharing a canonical directory.

    Each Bridge leases by workdir alias, not the filesystem inode. Two aliases
    in the same Bridge, or two independent Bridges, cannot both write the same
    directory/subtree without an additional cross-Bridge lock protocol.
    Read-only roots do not claim a writer lease.
    """
    from .workdirs import Workdir

    def writable_roots(items: tuple[Workdir, ...]) -> tuple[Path, ...]:
        roots: list[Path] = []
        for workdir in items:
            if not isinstance(workdir, Workdir):
                raise DarwinBridgeIdentityError("Bridge workdir claim has invalid type")
            if workdir.read_only:
                continue
            try:
                candidate = Path(workdir.root).expanduser().resolve(strict=True)
            except OSError as exc:
                raise DarwinBridgeIdentityError("Bridge writable workdir is unavailable") from exc
            if not candidate.is_dir():
                raise DarwinBridgeIdentityError("Bridge writable workdir is not a directory")
            roots.append(candidate)
        return tuple(roots)

    def overlaps(left: Path, right: Path) -> bool:
        # samefile on each ancestor accounts for case-insensitive macOS mounts
        # as well as aliases already canonicalized by Path.resolve().
        try:
            return any(os.path.samefile(a, right) for a in (left, *left.parents)) or any(
                os.path.samefile(a, left) for a in (right, *right.parents)
            )
        except OSError as exc:
            raise DarwinBridgeIdentityError("Bridge writable root identity is unavailable") from exc

    candidate = writable_roots(proposed)
    for index, left in enumerate(candidate):
        for right in candidate[index + 1 :]:
            if overlaps(left, right):
                raise DarwinBridgeIdentityError(
                    "same Bridge has overlapping writable workdir leases"
                )
    for other in other_bridges:
        for left in candidate:
            for right in writable_roots(other):
                if overlaps(left, right):
                    raise DarwinBridgeIdentityError(
                        "independent Bridges cannot claim overlapping writable workdirs"
                    )


def workdir_claims_from_private_bridge_config(path: Path) -> tuple[Workdir, ...]:
    """Read only writer-lease claims from a private Bridge policy document.

    The JSON may also contain secrets. Nothing except validated workdir roots
    and read-only flags is returned; the caller must not log the contents.
    An unknown owner/malformed policy refuses owned-Bridge startup.
    """
    from .workdirs import Workdir

    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise DarwinBridgeIdentityError("Bridge private policy path is unsafe")
    if any(parent.is_symlink() for parent in path.parents):
        raise DarwinBridgeIdentityError("Bridge private policy parent is a symlink")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DarwinBridgeIdentityError("Bridge private policy is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 262_144
    ):
        raise DarwinBridgeIdentityError("Bridge private policy owner or size is invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DarwinBridgeIdentityError("Bridge private policy is malformed") from exc
    if not isinstance(document, dict) or not isinstance(document.get("workdirs"), list):
        raise DarwinBridgeIdentityError("Bridge private policy has no workdir claims")
    claims: list[Workdir] = []
    for item in document["workdirs"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("alias"), str)
            or not isinstance(item.get("host_path"), str)
            or type(item.get("read_only")) is not bool
            or not Path(item["host_path"]).is_absolute()
        ):
            raise DarwinBridgeIdentityError("Bridge private workdir claim is invalid")
        claims.append(
            Workdir(
                alias=item["alias"],
                root=Path(item["host_path"]),
                description=None,
                read_only=item["read_only"],
            )
        )
    return tuple(claims)


def refuse_writable_conflicts_with_private_bridges(
    proposed: tuple[Workdir, ...],
    private_bridge_config_paths: tuple[Path, ...],
) -> None:
    """Fail closed if an independent Bridge already owns any writer root."""
    existing = tuple(
        workdir_claims_from_private_bridge_config(path) for path in private_bridge_config_paths
    )
    refuse_writable_workdir_overlaps(proposed, existing)


def preflight_named_owned_bridge(
    identity: InstanceIdentity,
    core_config: Path,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
    runtime_root: Path | None = None,
) -> dict[str, str | int]:
    """Read-only preflight before any named owned Bridge can be installed.

    The default Bridge's current private policy is mandatory. Every other
    named instance is inspected: disabled ones hold no Bridge writer lease;
    owned ones must have a valid independent private policy. A Shared Core
    has no Bridge process of its own, but its owner Bridge policy must be
    resolved and checked for writer overlap. Unknown owners fail closed.
    This is NOT a registration or grant.
    """
    from .darwin_instance_runtime import _read_collision_manifests
    from .native_config import load_native_config

    resources = named_owned_bridge_resources(identity, home=home, runtime_root=runtime_root)
    try:
        workdirs, settings = load_native_config(Path(core_config))
    except Exception as exc:
        raise DarwinBridgeIdentityError("named Bridge Core TOML cannot be validated") from exc
    if not settings.bridge_required or not any(
        item.agent_mode != "disabled" or item.policy.repo_ops_enabled for item in workdirs
    ):
        raise DarwinBridgeIdentityError(
            "owned Bridge requires explicitly enabled Agent or Repo Ops policy"
        )
    service_root = instance_service_home(identity, home=home)
    for item in workdirs:
        try:
            root = Path(item.root).resolve(strict=True)
        except OSError as exc:
            raise DarwinBridgeIdentityError("owned Bridge workdir is unavailable") from exc
        if root == service_root or root in service_root.parents or service_root in root.parents:
            raise DarwinBridgeIdentityError(
                "owned Bridge cannot expose private service state as a workdir"
            )

    if default_bridge_config is None:
        from .darwin_bridge_policy import bridge_config_path

        default_bridge_config = bridge_config_path()

    base = (Path.home() if home is None else Path(home)) / ("Library/Application Support/ServerFS")
    configs: list[Path] = [Path(default_bridge_config)]
    known_resources: list[NamedBridgeResources] = []
    shared_owners: dict[str, Path] = {}
    try:
        manifests = _read_collision_manifests(base, identity.instance_id, allow_candidate=True)
    except Exception as exc:
        raise DarwinBridgeIdentityError("named Bridge peer ownership cannot be verified") from exc
    for peer_id, document in manifests:
        if peer_id == DEFAULT_INSTANCE_ID:
            continue
        mode = document.get("bridge_mode")
        if mode == "disabled":
            continue
        if mode == "shared":
            # Shared Cores do not own a Bridge, but the referenced owner
            # claims writable roots. Silently skipping it allows two
            # independently started Bridges to write the same filesystem.
            from .darwin_instance_shared_manifest import _read_json
            from .darwin_shared_binding_private import load_shared_core_binding

            try:
                shared = InstanceIdentity(peer_id, "shared", document["bridge_id"])
                shared_home = instance_service_home(shared, home=home)
                binding_path = shared_home / "shared-bridge-binding.json"
                metadata = _read_json(binding_path)
                raw_socket = metadata.get("socket_path")
                if not isinstance(raw_socket, str) or not raw_socket.startswith("/"):
                    raise ValueError("missing private owner socket")
                binding = load_shared_core_binding(
                    identity=shared,
                    trusted_core_home=shared_home,
                    binding_path=binding_path,
                    expected_socket_path=Path(raw_socket),
                )
                owner_config = binding.bridge_config_path
                # Parse only a file that already satisfies the private
                # owner/mode/symlink checks used for writer claims.
                workdir_claims_from_private_bridge_config(owner_config)
                owner_doc = json.loads(owner_config.read_text(encoding="utf-8"))
                if (
                    owner_doc.get("protocol_mode") != "v2"
                    or owner_doc.get("bridge_id") != shared.bridge_id
                    or owner_doc.get("socket_path") != str(binding.socket_path)
                ):
                    raise ValueError("shared owner identity mismatch")
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                raise DarwinBridgeIdentityError(
                    "shared owner Bridge policy cannot be verified"
                ) from None
            if shared.bridge_id == identity.bridge_id:
                raise DarwinBridgeIdentityError(
                    "independent owned Bridge duplicates a shared owner identity"
                )
            previous = shared_owners.get(shared.bridge_id)
            if previous is not None and previous != owner_config:
                raise DarwinBridgeIdentityError("shared owner Bridge configuration is inconsistent")
            shared_owners[shared.bridge_id] = owner_config
            continue
        if mode != "owned" or document.get("bridge_id") != peer_id:
            raise DarwinBridgeIdentityError(
                "another named Bridge has an unrecognized ownership mode"
            )
        peer = InstanceIdentity(instance_id=peer_id, bridge_mode="owned", bridge_id=peer_id)
        known_resources.append(
            named_owned_bridge_resources(peer, home=home, runtime_root=runtime_root)
        )
        configs.append(
            instance_service_home(peer, home=home) / "data" / "agent-bridge" / "bridge.json"
        )
    configs.extend(shared_owners.values())
    refuse_bridge_resource_collisions(resources, tuple(known_resources))
    refuse_writable_conflicts_with_private_bridges(tuple(workdirs), tuple(configs))
    return {
        "instance_id": identity.instance_id,
        "bridge_mode": "owned",
        "workdirs_checked": len(workdirs),
        "peer_bridge_policies_checked": len(configs),
        "result": "PASS_SOURCE_PREFLIGHT_ONLY",
    }


def refuse_bridge_resource_collisions(
    proposed: NamedBridgeResources,
    others: tuple[NamedBridgeResources, ...],
) -> None:
    """Detect duplicate ownership before any bundle/Bridge is installed."""
    for existing in others:
        if proposed.bridge_id == existing.bridge_id or any(
            getattr(proposed, key) == getattr(existing, key)
            for key in ("socket_path", "state_dir", "lock_dir", "config_path")
        ):
            raise DarwinBridgeIdentityError("owned Bridge resource identity collision")
