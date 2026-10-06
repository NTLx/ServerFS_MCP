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
from .private_state import DirectoryMessages, ensure_private_directory, ensure_private_file
from .windows_security import current_user_sid

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
        read_only = bool(item.get("read_only", True))
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
        entry: dict[str, Any] = {"enabled": _strict_bool_field(policy, name, "enabled")}
        bin_key = _RUNTIME_BIN_KEY[name]
        entry[bin_key] = _strict_str(policy.get(bin_key), f"runtime {name} {bin_key}")
        if any(char in entry[bin_key] for char in ("\x00", "\n", "\r")):
            raise BridgeError(
                "BRIDGE_CONFIG_INVALID", f"runtime {name} {bin_key} contains an invalid character"
            )
        entry["use_proxy"] = _strict_bool_field(policy, name, "use_proxy")
        block[name] = entry
    return block


def _strict_bool_field(policy: dict[str, Any], name: str, key: str) -> bool:
    """A required JSON boolean. Absent or non-boolean is a refusal, never a coerced default."""
    value = policy.get(key)
    if type(value) is not bool:
        raise BridgeError("BRIDGE_CONFIG_INVALID", f"runtime {name} {key} must be a JSON boolean")
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
    """Write the config atomically, with the Bridge's own private-state contract applied.

    Publication is write-temp-then-``os.replace`` so a reader never observes a half-written JSON
    document, and the temp file is created inside the already-secured directory so it inherits the
    protected descriptor rather than landing in a world-readable temp location.
    """
    ensure_private_directory(path.parent, mode=0o700, messages=_STATE_MESSAGES)
    temp = path.parent / f".bridge.{secrets.token_hex(8)}.tmp"
    try:
        # CREATE_NEW semantics plus the private-state contract: the object is created with the
        # protected DACL and then verified, never silently re-secured.
        created = _create_private(temp)
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
        del created
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    # The published object is verified, not trusted: a pre-planted target must be refused (§25).
    ensure_private_file(path, mode=0o600, not_regular="the rendered Bridge config is unsafe")


def _create_private(path: Path) -> bool:
    from .windows_security import create_private_file, private_state_sddl

    if not sys.platform.startswith("win"):
        return False
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
