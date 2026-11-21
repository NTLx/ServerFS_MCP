"""Read-only exact-state diagnosis after the first I5 stage stopped at Core policy parsing.

Does not inspect credentials, register/stop/start services, repair data, or
make a provider request. Any deviation from the known one-use partial state
fails closed. Never rerun the original create-only installer.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
from pathlib import Path


class I5PostfailError(RuntimeError):
    """Sanitized post-failure state mismatch."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5PostfailError(reason)


def absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def historical_core_toml(fixture: Path) -> bytes:
    """Reconstruct the initial incomplete Core policy without using the new generator."""
    return (
        '[server]\nlog_level = "INFO"\n\n'
        "[agent]\nenabled = true\n\n"
        '[[workdirs]]\nalias = "Fixture"\n'
        "path = " + json.dumps(str(fixture), ensure_ascii=False) + "\n"
        'read_only = false\nagent_mode = "workspace-write"\n'
        'agent_runtimes = ["claude"]\nrepo_ops_enabled = false\n'
    ).encode("utf-8")


def inspect() -> None:
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_core_listener import _windows_ipv4_tcp_listeners
    from serverfs_mcp.windows_v25 import storage

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native non-elevated Windows -I -B required",
    )
    source = Path(__file__).with_name("v0250-i5-provider-shared-preflight.py")
    spec = importlib.util.spec_from_file_location("i5_partial_host_gate", source)
    require(spec is not None and spec.loader is not None, "I5 gate unavailable")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    require(gate.BRIDGE == "i5-shared", "I5 fixture namespace differs")
    require(
        gate.CORE_PORTS == {"i5-core-a": 18873, "i5-core-b": 18874},
        "I5 Core identities differ",
    )
    gate.protected_tasks()
    gate.verify_candidates(gate.candidates(), installed=False)
    print("I5_POSTFAIL_PROTECTED_AND_CANDIDATE_TASKS=EXPECTED")

    sid = current_user_sid()
    for instance in (gate.BRIDGE, *gate.CORE_PORTS):
        root = storage.root(instance)
        storage.check_private(root, directory=True, sid=sid)
        storage.check_private(root / "envs", directory=True, sid=sid)
    print("I5_POSTFAIL_THREE_PRIVATE_ROOTS=VERIFIED")

    bridge_root = storage.root(gate.BRIDGE)
    binaries = (
        bridge_root / "envs" / "bridge" / "Scripts" / "python.exe",
        bridge_root / "envs" / "agent-bridge" / "Scripts" / "python.exe",
        *(
            storage.root(core_id) / "envs" / "core" / "Scripts" / "python.exe"
            for core_id in gate.CORE_PORTS
        ),
    )
    for binary in binaries:
        storage.check_private(binary, directory=False, sid=sid)
    print("I5_POSTFAIL_FOUR_PRIVATE_VENVS=VERIFIED")

    bridge_home = storage.home("bridge", gate.BRIDGE)
    bridge_config = bridge_home / "bridge.json"
    fixture = bridge_root / "workdirs" / "fixture"
    storage.check_private(bridge_config, directory=False, sid=sid)
    storage.check_private(fixture, directory=True, sid=sid)
    require(0 < bridge_config.stat().st_size < 256 * 1024, "I5 Bridge config invalid size")
    config = json.loads(bridge_config.read_text(encoding="utf-8"))
    require(
        config.get("bridge_id") == gate.BRIDGE
        and isinstance(config.get("claude"), dict)
        and config["claude"].get("enabled") is True
        and isinstance(config.get("workdirs"), list)
        and len(config["workdirs"]) == 1
        and config["workdirs"][0].get("host_path") == str(fixture),
        "I5 installed Bridge policy does not match expected fixture",
    )
    print("I5_POSTFAIL_BRIDGE_CLAUDE_POLICY=VERIFIED")

    first_id, second_id = tuple(gate.CORE_PORTS)
    first_home = storage.home("core", first_id)
    second_home = storage.home("core", second_id)
    first_toml = first_home / "serverfs.toml"
    storage.check_private(first_toml, directory=False, sid=sid)
    require(
        first_toml.read_bytes() == historical_core_toml(fixture),
        "I5 initial Core policy is not the exact historical failure fixture",
    )
    print("I5_POSTFAIL_CORE_A_ORIGINAL_POLICY=EXACT")

    unused = [
        first_home / "http.env",
        first_home / "bridge-binding.json",
        first_home / "serverfs.original-invalid.toml",
        second_home / "serverfs.toml",
        second_home / "http.env",
        second_home / "bridge-binding.json",
        bridge_home / "state" / "core-grants.json",
    ]
    for component, instance in (
        ("bridge", gate.BRIDGE),
        *(("core", core_id) for core_id in gate.CORE_PORTS),
    ):
        home = storage.home(component, instance)
        unused.extend((home / "service.json", home / "task.xml"))
    require(all(absent(path) for path in unused), "I5 partial installation is not exact")
    print("I5_POSTFAIL_LATER_CREDENTIALS_GRANTS_AND_MANIFESTS=ABSENT")

    listeners = _windows_ipv4_tcp_listeners()
    require(
        not any(listener.port in gate.CORE_PORTS.values() for listener in listeners),
        "I5 partial Core TCP port is unexpectedly occupied",
    )
    print("I5_POSTFAIL_PORTS=UNBOUND")
    print("I5_EXACT_PARTIAL_STATE_READONLY=PASS")


if __name__ == "__main__":
    try:
        inspect()
    except BaseException as exc:  # noqa: BLE001 - only class, no private inputs
        print("I5_POSTFAIL_READONLY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
