"""macOS Bridge policy translation and private POSIX config publication."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from .config import _load_acp_profiles, _load_repo_actions
from .errors import BridgeError
from .models import KNOWN_RUNTIME_NAMES, AgentMode
from .policy import WorkdirAgentPolicy
from .private_state import (
    DirectoryMessages,
    ensure_private_directory,
    ensure_private_file,
    verify_private_file,
)

#: The native lease key (§5.3). A native deployment has no legacy slot, so leases are keyed by the
#: exact alias and both sides derive the artifact name independently.
NATIVE_LEASE_KEY = "alias"

#: A rendered document is operator policy, so it is bounded like any other bounded input.
MAX_INPUT_BYTES = 256 * 1024

_INPUT_KEYS = frozenset({"workdirs", "enable_fake_runtime", "limits", "runtimes", "acp_profiles"})
_WORKDIR_KEYS = frozenset(
    {
        "alias",
        "host_path",
        "read_only",
        "agent_mode",
        "agent_runtimes",
        "repo_ops_enabled",
        "repo_actions",
    }
)

#: Per-runtime policy keys the renderer accepts. These are all non-secret: an executable name and
#: two booleans. The proxy *endpoint* is deliberately absent — it arrives over the bootstrap channel
#: and must never be persisted here (§15 D2/D4).
_RUNTIME_KEYS: dict[str, frozenset[str]] = {
    "codex": frozenset({"enabled", "codex_bin", "use_proxy"}),
    "claude": frozenset({"enabled", "claude_bin", "use_proxy"}),
    "qoder": frozenset({"enabled", "qoder_bin", "use_proxy"}),
}
#: Each runtime's executable key, matching the Bridge's own settings field names so the document
#: maps one to one with no translation table.
_RUNTIME_BIN_KEY = {"codex": "codex_bin", "claude": "claude_bin", "qoder": "qoder_bin"}

_STATE_MESSAGES = DirectoryMessages(
    not_a_directory="the Bridge state directory is not a directory",
    not_owned="the Bridge state directory is not owned by this user",
)


def _strict_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError("BRIDGE_CONFIG_INVALID", f"{label} must be a non-empty string")
    return value


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise BridgeError("BRIDGE_CONFIG_INVALID", f"unknown {label} field: {sorted(unknown)[0]}")


def _validate_workdirs(entries: Any) -> list[WorkdirAgentPolicy]:
    """Validate the workdir policy exactly as the Bridge's own loader will.

    The renderer does not get its own relaxed dialect: a document that the Bridge would later refuse
    must fail here, at startup, rather than after the Bridge process has been spawned.
    """
    if not isinstance(entries, list) or not entries:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "workdirs must be a non-empty array")
    policies: list[WorkdirAgentPolicy] = []
    seen_aliases: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise BridgeError("BRIDGE_CONFIG_INVALID", "workdirs entries must be objects")
        _reject_unknown(item, _WORKDIR_KEYS, "workdir")
        if "slot" in item:
            # Native workdirs carry no legacy slot (§6). Its presence would mean the two sides key
            # the lease differently and lock two different files while both report success (§5.3).
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID",
                "a native workdir entry must not carry a slot; leases key on the alias",
            )
        alias = _strict_str(item.get("alias"), "workdir.alias")
        if alias in seen_aliases:
            raise BridgeError("BRIDGE_CONFIG_INVALID", f"duplicate workdir alias: {alias}")
        seen_aliases.add(alias)
        host_path = Path(_strict_str(item.get("host_path"), "workdir.host_path"))
        if not host_path.is_absolute():
            raise BridgeError("BRIDGE_CONFIG_INVALID", "workdir.host_path must be absolute")
        runtimes = item.get("agent_runtimes", [])
        if not isinstance(runtimes, list):
            raise BridgeError("BRIDGE_CONFIG_INVALID", "workdir.agent_runtimes must be an array")
        for runtime in runtimes:
            if runtime not in KNOWN_RUNTIME_NAMES:
                raise BridgeError("BRIDGE_CONFIG_INVALID", f"unknown agent runtime: {runtime}")
        # AgentMode validates the frozen vocabulary here, so an unknown mode is refused at startup
        # instead of reaching the Bridge as a string it would reject later. The enum raises a bare
        # ValueError, which is translated so every refusal from this entry point is a BridgeError —
        # a raw ValueError would escape the CLI's redacted-error path and print its own message.
        try:
            mode = AgentMode(
                _strict_str(item.get("agent_mode", AgentMode.DISABLED.value), "agent_mode")
            )
        except ValueError as exc:
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "workdir.agent_mode is not a known agent mode"
            ) from exc
        read_only = (
            _strict_bool_field(item, "workdir", "read_only") if "read_only" in item else True
        )
        if mode is AgentMode.DISABLED and runtimes:
            raise BridgeError("BRIDGE_CONFIG_INVALID", "disabled agent mode cannot allow runtimes")
        if mode is AgentMode.WORKSPACE_WRITE and read_only:
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "workspace-write agent mode requires a writable workdir"
            )
        repo_ops_enabled = item.get("repo_ops_enabled", False)
        if type(repo_ops_enabled) is not bool:
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "workdir.repo_ops_enabled must be a JSON boolean"
            )
        try:
            repo_actions = _load_repo_actions(
                item.get("repo_actions"), enabled=repo_ops_enabled, read_only=read_only
            )
        except ValueError as exc:
            raise BridgeError("BRIDGE_CONFIG_INVALID", str(exc)) from exc
        policies.append(
            WorkdirAgentPolicy(
                slot=None,  # native deployment: alias-derived identity only
                alias=alias,
                host_path=host_path,
                mode=mode,
                runtimes=frozenset(runtimes),
                read_only=read_only,
                repo_ops_enabled=repo_ops_enabled,
                repo_actions=repo_actions,
            )
        )
    return policies


def _runtime_block(runtimes: object) -> dict[str, dict[str, Any]]:
    """The per-runtime block, in the shape the Bridge config already uses.

    ``runtimes`` is either the legacy list of enabled names — kept so an existing caller and the
    Linux deployment keep working unchanged — or a mapping of name to its non-secret policy. The
    mapping form is what carries ``*_bin`` and ``use_proxy`` through to ``CodexSettings`` /
    ``ClaudeSettings`` / ``QoderSettings``; sending only names would silently drop both, and
    ``use_proxy`` is what decides whether a provider child gets the Agent proxy at all.
    """
    if runtimes is None:
        return {}
    if isinstance(runtimes, list):
        block: dict[str, dict[str, Any]] = {}
        for name in runtimes:
            if not isinstance(name, str) or name not in _RUNTIME_KEYS:
                raise BridgeError("BRIDGE_CONFIG_INVALID", f"unknown agent runtime: {name}")
            block[name] = {"enabled": True}
        return block
    if not isinstance(runtimes, dict):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "runtimes must be a list or an object")
    block = {}
    for name, policy in runtimes.items():
        if name not in _RUNTIME_KEYS:
            raise BridgeError("BRIDGE_CONFIG_INVALID", f"unknown agent runtime: {name}")
        if not isinstance(policy, dict):
            raise BridgeError("BRIDGE_CONFIG_INVALID", f"runtime {name} policy must be an object")
        _reject_unknown(policy, _RUNTIME_KEYS[name], f"runtime {name}")
        entry: dict[str, Any] = {
            "enabled": _strict_bool_field(policy, f"runtime {name}", "enabled")
        }
        bin_key = _RUNTIME_BIN_KEY[name]
        entry[bin_key] = _strict_str(policy.get(bin_key), f"runtime {name} {bin_key}")
        if any(char in entry[bin_key] for char in ("\x00", "\n", "\r")):
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", f"runtime {name} {bin_key} contains an invalid character"
            )
        entry["use_proxy"] = _strict_bool_field(policy, f"runtime {name}", "use_proxy")
        block[name] = entry
    return block


def _strict_bool_field(data: dict[str, Any], label: str, key: str) -> bool:
    """A required JSON boolean. Absent or non-boolean is a refusal, never a coerced default.

    ``bool("false")`` is True in Python, so a string arriving in the render request would silently
    invert the policy. The Bridge's own loader already applies this rule to ``read_only``; using it
    here keeps the renderer from accepting what the loader would later refuse.
    """
    value = data.get(key)
    if type(value) is not bool:
        raise BridgeError("BRIDGE_CONFIG_INVALID", f"{label} {key} must be a JSON boolean")
    return value


def build_policy_document(request: dict[str, Any]) -> dict[str, Any]:
    """Build the non-secret Bridge policy derived from operator configuration.

    Native deployments have one operator-facing policy source (``serverfs.toml``).  The platform
    lifecycle may persist additional private material beside that derived policy (for example Jev
    credentials or a proxy endpoint), so this helper deliberately emits only fields owned by the
    operator policy.  Both the Windows renderer and the macOS policy synchronizer use this exact
    projection; they must never grow independent workdir/runtime translations.
    """
    _reject_unknown(request, _INPUT_KEYS, "render request")
    policies = _validate_workdirs(request.get("workdirs"))
    runtimes = request.get("runtimes", [])
    document: dict[str, Any] = {
        "lease_key": NATIVE_LEASE_KEY,
        "enable_fake_runtime": bool(request.get("enable_fake_runtime", False)),
        "workdirs": [
            {
                "alias": policy.alias,
                "host_path": str(policy.host_path),
                "read_only": policy.read_only,
                "agent_mode": policy.mode.value,
                "agent_runtimes": sorted(policy.runtimes),
                **(
                    {
                        "repo_ops_enabled": True,
                        "repo_actions": {
                            action.name: {
                                "argv": list(action.argv),
                                "mutates": action.mutates,
                                "argument": action.argument,
                                "integrity": dict(action.integrity),
                                **({"kind": action.kind} if action.kind != "command" else {}),
                                **({"repository": action.repository} if action.repository else {}),
                                **(
                                    {"github_cli": action.github_cli}
                                    if action.github_cli != "gh"
                                    else {}
                                ),
                            }
                            for action in policy.repo_actions
                        },
                    }
                    if policy.repo_ops_enabled
                    else {}
                ),
            }
            for policy in policies
        ],
    }
    if isinstance(request.get("limits"), dict):
        document["limits"] = request["limits"]
    document.update(_runtime_block(runtimes))
    if "acp_profiles" in request:
        profiles = _load_acp_profiles(request["acp_profiles"])
        document["acp_profiles"] = [
            {
                "id": profile.id,
                "command": profile.command,
                "args": list(profile.args),
                "use_proxy": profile.use_proxy,
                "probe_timeout_seconds": profile.probe_timeout_seconds,
                "shutdown_timeout_seconds": profile.shutdown_timeout_seconds,
            }
            for profile in sorted(profiles, key=lambda profile: profile.id)
        ]
    return document


def _publish_private_file(path: Path, text: str) -> None:
    """Write the config atomically, verifying an existing target *before* replacing it.

    The ordering here is the security property. §25 says an unsafe existing object is refused and
    never repaired, and an earlier version of this function replaced the target first and verified
    afterwards — which meant a pre-planted broad-DACL or reparse ``bridge.json`` was overwritten
    before anything looked at it. Replacing an attacker-controlled object is exactly the outcome
    §25 exists to prevent, so the check happens first and a refusal leaves the target untouched.

    The logic is the Bridge's own ``private_state`` helpers rather than a second copy of the ACL
    rules (§15 D2). Publication stays write-temp-then-``os.replace`` so a reader never observes a
    half-written document, and the temp file is created inside the already-secured directory so it
    inherits the protected descriptor instead of landing in a world-readable temp location.
    """
    ensure_private_directory(path.parent, mode=0o700, messages=_STATE_MESSAGES, parents=True)

    # Fail closed on an existing object we could not vouch for, *before* writing anything. A
    # reparse point, a non-regular file or a foreign/broad DACL all leave the target exactly as they
    # were: no temp file is created and no replacement happens.
    #
    # The regular-file check is explicit because ``verify_private_file``'s Windows branch verifies
    # the descriptor but not the object type; without it a directory planted at bridge.json reached
    # ``os.replace``, which then failed with a raw PermissionError after the temp file was already
    # written. Refusing here is what makes the guarantee "the target is untouched".
    if _is_reparse(path):
        # Refused before anything else: a dangling symlink reports exists() == False, so testing
        # existence first would let the cheapest planted case through.
        raise BridgeError("BRIDGE_CONFIG_INVALID", "the Bridge config path is a reparse point")
    if path.exists():
        if path.is_dir():
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", "the Bridge config path is a directory, not a file"
            )
        verify_private_file(
            path,
            not_regular="the existing Bridge config is not a regular file",
            not_private="the existing Bridge config is not private",
        )

    temp = path.parent / f".bridge.{secrets.token_hex(8)}.tmp"
    try:
        _create_private(temp)
        fd = os.open(temp, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        ensure_private_file(
            temp,
            mode=0o600,
            not_regular="the rendered Bridge config is not a regular file",
        )
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    # The object this call published is verified too, so a substitution between the two steps would
    # still be caught before the Bridge is told the path.
    verify_private_file(
        path,
        not_regular="the rendered Bridge config is not a regular file",
        not_private="the rendered Bridge config is not private",
    )


def _is_reparse(path: Path) -> bool:
    """Reject an existing symlink, including a dangling one, before publication."""
    return path.is_symlink()


def _create_private(path: Path) -> bool:
    # A new file is exclusive and private from its first inode.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return True


if __name__ == "__main__":
    raise SystemExit("Windows Bridge config renderer is not supported on this platform")
