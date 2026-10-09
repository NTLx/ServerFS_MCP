"""Render the private Bridge configuration from an operator config (v0.11 Phase D, §15 D2).

The operator maintains exactly one file, ``serverfs.toml``. This module is the Bridge-owned half of
turning it into the Bridge's internal JSON, and it runs **inside the agent_bridge package** as its
own CLI entry point for two reasons that are architectural rather than stylistic:

- **§23/§70 freeze the packages as independent.** ``serverfs_mcp`` must not import
  ``serverfs_agent_bridge``, and a renderer living in the MCP package would need either that import
  or a second copy of the ACL logic. Phase B already established the parity-table pattern for the
  two sides that genuinely must agree; here there is nothing to agree on, because the Bridge simply
  does the writing with the helpers it already owns.
- **The generated file is private state, not an ordinary config.** It must carry a protected DACL,
  a verified owner SID, reparse refusal and atomic publication. That is ``private_state`` and
  ``windows_security`` verbatim (§15 D2 forbids restating raw ACL code), and those modules live in
  this package.

Security properties this entry point is responsible for:

- ``lease_key`` is always ``alias`` and a native workdir entry never carries ``slot``. A native
  deployment has no slots (§5.3), so a rendered ``slot`` would key the lease differently from the
  ServerFS reader and silently lock two different files.
- ``allowed_peer_sid`` is read from the live process identity via the Phase B Windows helper. It is
  never hardcoded, never derived from a username, and the pipe name is never treated as
  authentication (§4.4).
- **No secret is ever written here.** The Agent proxy endpoint is *not* part of this document — it
  arrives over the private bootstrap channel (§15 D4) and lives only in the Bridge's memory. The
  rendered JSON therefore contains policy and identity, never a credential, and a test asserts the
  absence directly.

Input arrives as a bounded JSON document on stdin so no intermediate file with the same content ever
exists on disk, and output is a single redacted summary line the supervisor parses.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import lease_identity
from .data_home import bridge_data_home
from .errors import BridgeError
from .local_ipc import derive_pipe_name
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

_INPUT_KEYS = frozenset({"workdirs", "enable_fake_runtime", "limits", "runtimes"})
_WORKDIR_KEYS = frozenset({"alias", "host_path", "read_only", "agent_mode", "agent_runtimes"})

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


@dataclass(frozen=True)
class RenderedBridgeConfig:
    """Where the Bridge will run and what it was told to allow."""

    config_path: Path
    state_dir: Path
    lock_dir: Path
    socket_path: Path
    allowed_peer_sid: str
    lease_ids: tuple[str, ...]


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
        policies.append(
            WorkdirAgentPolicy(
                slot=None,  # native deployment: alias-derived identity only
                alias=alias,
                host_path=host_path,
                mode=mode,
                runtimes=frozenset(runtimes),
                read_only=read_only,
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


def build_config_document(
    request: dict[str, Any],
    *,
    socket_path: Path,
    state_dir: Path,
    lock_dir: Path,
    peer_sid: str,
) -> dict[str, Any]:
    """Assemble the Bridge JSON document. Contains policy and identity, never a credential."""
    _reject_unknown(request, _INPUT_KEYS, "render request")
    policies = _validate_workdirs(request.get("workdirs"))
    # Shape validation belongs to _runtime_block, which accepts both the legacy name list and the
    # policy mapping. Validating it as an array here would reject the mapping form.
    runtimes = request.get("runtimes", [])
    document: dict[str, Any] = {
        "lease_key": NATIVE_LEASE_KEY,
        "socket_path": str(socket_path),
        "state_dir": str(state_dir),
        "lock_dir": str(lock_dir),
        # Identity comes from the live process, never from a username and never hardcoded (§4.4).
        "allowed_peer_sid": peer_sid,
        "enable_fake_runtime": bool(request.get("enable_fake_runtime", False)),
        "workdirs": [
            {
                "alias": policy.alias,
                "host_path": str(policy.host_path),
                "read_only": policy.read_only,
                "agent_mode": policy.mode.value,
                "agent_runtimes": sorted(policy.runtimes),
            }
            for policy in policies
        ],
    }
    if isinstance(request.get("limits"), dict):
        document["limits"] = request["limits"]
    document.update(_runtime_block(runtimes))
    return document


def render_native_bridge_config(
    request: dict[str, Any], *, home: Path | None = None
) -> RenderedBridgeConfig:
    """Render, secure and publish the private Bridge configuration.

    ``home`` overrides the Bridge data home for tests; production resolves it from
    ``SERVERFS_DATA_HOME`` / ``LOCALAPPDATA`` (§6.2).
    """
    data_home = home if home is not None else bridge_data_home()
    state_dir = data_home / "state"
    lock_dir = data_home / "locks"
    # Imported here rather than at module scope: ``windows_security`` calls ``ctypes.WinDLL``
    # while being imported, which does not exist off Windows, so a top-level import made this
    # module unimportable there. ``private_state`` already guards the same seam with ``WINDOWS``;
    # this keeps the renderer consistent with it. The renderer is Windows-only in practice -- the
    # SID and the pipe name it produces are the native deployment's -- but the *module* must
    # still import so the Linux Bridge suite can collect it.
    from .windows_security import current_user_sid

    peer_sid = current_user_sid()
    socket_path = Path(derive_pipe_name(peer_sid))

    document = build_config_document(
        request,
        socket_path=socket_path,
        state_dir=state_dir,
        lock_dir=lock_dir,
        peer_sid=peer_sid,
    )
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"

    # The directory tree is created and secured before the file, so the file is never created inside
    # an inherited descriptor. ensure_private_directory creates with an explicit protected DACL and
    # verifies rather than repairs an object it did not create (§25).
    for directory in (data_home, state_dir, lock_dir):
        ensure_private_directory(directory, mode=0o700, messages=_STATE_MESSAGES, parents=True)

    config_path = data_home / "bridge.json"
    _publish_private_file(config_path, encoded)

    return RenderedBridgeConfig(
        config_path=config_path,
        state_dir=state_dir,
        lock_dir=lock_dir,
        socket_path=socket_path,
        allowed_peer_sid=peer_sid,
        lease_ids=tuple(
            lease_identity.alias_lease_id(str(entry["alias"])) for entry in document["workdirs"]
        ),
    )


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
    ensure_private_directory(path.parent, mode=0o700, messages=_STATE_MESSAGES)

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
    """Whether the path is a reparse point, including one whose target does not exist.

    The ``path.exists()`` guard that used to precede the Windows call was a real defect, not a
    redundancy. ``Path.exists()`` follows the link, so a *dangling* symlink — one whose target is
    absent — reports False while ``lstat`` still reports the reparse tag. Prefixing the
    check with ``exists()`` therefore classified exactly the case an attacker can plant cheaply and
    invisibly as "nothing there", and the publication proceeded to write through it.

    ``windows_security.is_reparse_point`` already answers the three cases correctly: a genuinely
    absent path is False (``lstat`` raises ``FileNotFoundError``), an existing reparse point is
    True from the tag or ``FILE_ATTRIBUTE_REPARSE_POINT``, and an inspection failure is refused
    closed by raising. Adding a pre-check can only lose information, so none is added.
    """
    import sys

    if sys.platform != "win32":
        # POSIX has no reparse points; a symlink is the equivalent, and lstat is the right question.
        return path.is_symlink()
    from . import windows_security

    return windows_security.is_reparse_point(path)


def _create_private(path: Path) -> bool:
    if not sys.platform.startswith("win"):
        return False
    # Imported here for the same reason as in ``render_native_bridge_config``: the module must stay
    # importable off Windows, and this function is the Windows-only one that needs the Win32 layer.
    from .windows_security import (
        create_private_file,
        current_user_sid,
        private_state_sddl,
    )

    # The DACL names the Bridge user alone (§6.1): the frozen "Bridge user alone" trustee is
    # the TokenUser, not the token owner. On a normal user token the two are identical; on an
    # elevated process the token owner is the Administrators group, and using it here would
    # widen the descriptor from one user to a whole group. Ownership of the created object is
    # left to the Windows default and verified against the token owner separately (measured
    # Phase H: the elevated CI runner's TokenUser and TokenOwner differ).
    return create_private_file(path, private_state_sddl(current_user_sid()))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: bounded JSON on stdin, one redacted summary line on stdout."""
    parser = argparse.ArgumentParser(
        prog="serverfs-agent-bridge-render-config",
        description="Render the private Agent Bridge configuration from operator policy",
    )
    parser.add_argument(
        "--data-home",
        type=Path,
        default=None,
        help="override the Bridge data home (tests); production resolves it per §6.2",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        print("render request exceeds the bounded input size", file=sys.stderr)
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("render request must be a JSON object")
        rendered = render_native_bridge_config(request, home=args.data_home)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"render request is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except BridgeError as exc:
        # BridgeError messages are redacted by construction and name no host path or secret.
        print(f"bridge configuration refused: {exc}", file=sys.stderr)
        return 2

    # The summary carries only non-secret placement facts. It is what the supervisor needs to wire
    # the ServerFS child, and it deliberately omits every credential-bearing value.
    print(
        json.dumps(
            {
                "config_path": str(rendered.config_path),
                "state_dir": str(rendered.state_dir),
                "lock_dir": str(rendered.lock_dir),
                "socket_path": str(rendered.socket_path),
                "allowed_peer_sid": rendered.allowed_peer_sid,
                "lease_ids": list(rendered.lease_ids),
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
