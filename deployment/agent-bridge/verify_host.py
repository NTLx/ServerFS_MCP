#!/usr/bin/env python3
"""Verify the user-scoped ServerFS Agent Bridge deployment invariants."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any


class VerifyError(RuntimeError):
    pass


def _home() -> Path:
    home = Path.home()
    if not home.is_absolute():
        raise VerifyError("user home must be absolute")
    return home


def _config_path() -> Path:
    return _home() / ".config/serverfs-agent-bridge/config.json"


def _provider_env() -> Path:
    return _home() / ".config/serverfs-agent-bridge/provider.env"


def _unit_path() -> Path:
    return _home() / ".config/systemd/user/serverfs-agent-bridge.service"


def _current_app() -> Path:
    return _home() / ".local/share/serverfs-agent-bridge/current"


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise VerifyError(f"missing required path: {path}") from exc


def _require_directory(path: Path, *, mode: int | None = None) -> None:
    st = _lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VerifyError(f"{path} must be a real directory")
    _require_owner(path, st)
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise VerifyError(f"{path}: mode {stat.S_IMODE(st.st_mode):04o} != expected {mode:04o}")


def _require_regular_file(path: Path, *, mode: int | None = None) -> None:
    st = _lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise VerifyError(f"{path} must be a regular file")
    _require_owner(path, st)
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise VerifyError(f"{path}: mode {stat.S_IMODE(st.st_mode):04o} != expected {mode:04o}")


def _require_owner(path: Path, st: os.stat_result) -> None:
    if st.st_uid != os.getuid():
        raise VerifyError(f"{path}: not owned by the current user")
    if st.st_gid != os.getgid():
        raise VerifyError(f"{path}: group is not the current primary group")


def _read_config() -> dict[str, Any]:
    path = _config_path()
    _require_regular_file(path, mode=0o600)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError("cannot read valid Bridge config.json") from exc
    if not isinstance(value, dict):
        raise VerifyError("Bridge config.json must be an object")
    return value


def _probe_bridge(socket_path: Path) -> dict[str, Any]:
    request = {
        "protocol_version": 1,
        "request_id": f"verify_{os.getpid()}",
        "method": "runtime.list",
        "params": {},
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(socket_path))
        client.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        response = b""
        while b"\n" not in response and len(response) <= 1_048_576:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    finally:
        client.close()

    try:
        payload = json.loads(response)
    except json.JSONDecodeError as exc:
        raise VerifyError("Bridge did not return a valid JSON response") from exc
    if not isinstance(payload, dict) or payload.get("request_id") != request["request_id"]:
        raise VerifyError("Bridge response request_id mismatch")
    if payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
        raise VerifyError(f"Bridge runtime.list failed: {payload!r}")
    return payload["result"]


def _wait_for_bridge_ready(
    socket_path: Path,
    *,
    timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 0.1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_transient_error: BaseException | None = None

    while True:
        try:
            socket_stat = socket_path.lstat()
        except FileNotFoundError as exc:
            last_transient_error = exc
        else:
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise VerifyError("bridge.sock must be a Unix socket")
            _require_owner(socket_path, socket_stat)
            if stat.S_IMODE(socket_stat.st_mode) != 0o660:
                raise VerifyError("bridge.sock must be mode 0660")
            try:
                _probe_bridge(socket_path)
            except (FileNotFoundError, ConnectionError, TimeoutError) as exc:
                last_transient_error = exc
            else:
                return

        if time.monotonic() >= deadline:
            if isinstance(last_transient_error, FileNotFoundError):
                raise VerifyError(
                    f"bridge.sock did not appear within {timeout_seconds:g} seconds"
                ) from last_transient_error
            raise VerifyError(
                f"Bridge RPC did not become ready within {timeout_seconds:g} seconds"
            ) from last_transient_error

        time.sleep(retry_interval_seconds)


def _wait_for_enabled_runtimes_ready(
    socket_path: Path,
    enabled_runtimes: set[str],
    *,
    timeout_seconds: float = 10.0,
    retry_interval_seconds: float = 0.25,
) -> None:
    if not enabled_runtimes:
        return

    deadline = time.monotonic() + timeout_seconds
    last_unready = sorted(enabled_runtimes)
    last_transient_error: BaseException | None = None

    while True:
        try:
            result = _probe_bridge(socket_path)
        except (FileNotFoundError, ConnectionError, TimeoutError) as exc:
            last_transient_error = exc
        else:
            runtimes = result.get("runtimes")
            if not isinstance(runtimes, list):
                raise VerifyError("Bridge runtime.list returned an invalid runtimes payload")
            available = {
                item.get("name")
                for item in runtimes
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("available") is True
            }
            last_unready = sorted(enabled_runtimes - available)
            if not last_unready:
                return
            last_transient_error = None

        if time.monotonic() >= deadline:
            names = ", ".join(last_unready) if last_unready else "unknown"
            message = (
                f"enabled runtimes did not become ready within {timeout_seconds:g} seconds: {names}"
            )
            raise VerifyError(message) from last_transient_error

        time.sleep(retry_interval_seconds)


def verify(*, require_runtimes: bool = False) -> None:
    if os.geteuid() == 0:
        raise VerifyError("run verification as the normal login user, not root")

    config = _read_config()
    if config.get("allowed_peer_uid") != os.getuid():
        raise VerifyError("allowed_peer_uid must equal the current user id")
    if config.get("allowed_peer_gid") != os.getgid():
        raise VerifyError("allowed_peer_gid must equal the current primary group id")
    if config.get("enable_fake_runtime") is not False:
        raise VerifyError("deployed config must disable fake runtime")

    socket_path = Path(str(config.get("socket_path", "")))
    lock_dir = Path(str(config.get("lock_dir", "")))
    state_dir = Path(str(config.get("state_dir", "")))
    data_root = _home() / ".local/share/serverfs-agent-bridge"
    expected_socket_dir = data_root / "runtime/socket"
    expected_lock_dir = data_root / "runtime/locks"
    expected_state_dir = _home() / ".local/state/serverfs-agent-bridge"

    if socket_path != expected_socket_dir / "bridge.sock":
        raise VerifyError("socket_path does not match the current user runtime directory")
    if lock_dir != expected_lock_dir:
        raise VerifyError("lock_dir does not match the current user runtime directory")
    if state_dir != expected_state_dir:
        raise VerifyError("state_dir does not match the current user state directory")

    _require_directory(expected_socket_dir, mode=0o750)

    # systemd Type=simple considers the service started as soon as the process
    # is spawned. The Bridge creates its lock files and binds its Unix socket
    # just after that point, so wait for a successful RPC before checking the
    # complete runtime asset set. Structural/permission/protocol errors still
    # fail immediately.
    _wait_for_bridge_ready(socket_path)

    _require_directory(expected_lock_dir, mode=0o750)
    lock_files = sorted(expected_lock_dir.glob("*.lock"))
    expected_names = [f"{slot:02d}.lock" for slot in range(1, 17)]
    if [path.name for path in lock_files] != expected_names:
        raise VerifyError("lock directory must contain exactly 01.lock through 16.lock")
    for path in lock_files:
        _require_regular_file(path, mode=0o640)

    _require_directory(expected_state_dir, mode=0o700)
    _require_regular_file(_provider_env(), mode=0o600)
    _require_regular_file(_unit_path(), mode=0o644)

    current = _current_app()
    if not current.is_symlink():
        raise VerifyError("current application must be a symlink")
    target = current.resolve(strict=True)
    releases = current.parent / "releases"
    try:
        target.relative_to(releases)
    except ValueError as exc:
        raise VerifyError("current application target escapes the releases directory") from exc
    entrypoint = current / ".venv/bin/serverfs-agent-bridge"
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        raise VerifyError("installed Agent Bridge entrypoint is missing or not executable")

    enabled_runtimes: set[str] = set()
    for provider in ("codex", "claude"):
        section = config.get(provider)
        if not isinstance(section, dict):
            raise VerifyError(f"missing {provider} config section")
        if not section.get("enabled"):
            continue
        enabled_runtimes.add(provider)
        key = "codex_bin" if provider == "codex" else "claude_bin"
        raw = section.get(key)
        if not isinstance(raw, str) or not raw:
            raise VerifyError(f"enabled {provider} has no executable path")
        binary = Path(raw)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise VerifyError(f"{provider} executable is unavailable")

    if require_runtimes:
        _wait_for_enabled_runtimes_ready(socket_path, enabled_runtimes)

    print(f"user_uid={os.getuid()}")
    print(f"user_gid={os.getgid()}")
    print(f"socket={socket_path}")
    print(f"state={state_dir}")
    print("bridge_rpc=PASS")
    if require_runtimes:
        print("enabled_runtimes=PASS:" + ",".join(sorted(enabled_runtimes)))
    print("user_scoped_deployment=PASS")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the user-scoped ServerFS Agent Bridge deployment."
    )
    parser.add_argument(
        "--require-runtimes",
        action="store_true",
        help="wait up to 10 seconds for every enabled native runtime to report available",
    )
    args = parser.parse_args()
    try:
        verify(require_runtimes=args.require_runtimes)
    except (VerifyError, OSError, TimeoutError) as exc:
        raise SystemExit(f"user_scoped_deployment=FAIL: {exc}") from exc


if __name__ == "__main__":
    main()
