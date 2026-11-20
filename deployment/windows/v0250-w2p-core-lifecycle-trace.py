"""Trace a single W2i independent Core start without changing Product source.

This runs INSIDE the installed private Core console environment. Tracing
wraps only existing Python functions; it never substitutes, bypasses or
weakens the Core owner checks or Task Scheduler operations. Every recorded
value is from a fixed vocabulary; no user identities, paths, arguments,
secrets, process IDs, raw stderr or private status dictionaries are exposed.

Invoke ONLY through v0250-w2i-core-smoke.py with its established outer
exact-Core-only disabled-task fallback and legacy/sibling task fences.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_EVENTS = 48
_ALLOWED_OPS = frozenset({"query", "change-enable", "change-disable", "run", "end"})
_ALLOWED_REASONS = frozenset(
    {
        "Core activation failed; rollback incomplete",
        "Core registered task identity has drifted",
        "Core Task Scheduler task is absent or query failed",
        "Core Task Scheduler returned an empty definition",
        "Core Task Scheduler mutation failed",
        "Core Task Scheduler operation failed",
        "Core source task XML failed identity validation",
        "Core task is not registered",
        "Core service manifest is unreadable",
        "Core service manifest is invalid or belongs to another user",
        "Core service private Windows DACL validation failed",
        "Core registered task did not become enabled",
        "Core registered owner drifted during lifecycle change",
        "Core lifecycle proof timed out",
        "Core previous generation history is unverified",
        "Core previous generation is still alive",
        "Core previous process termination is unverified",
        "Core current-user SID is unavailable",
        "Core Task Scheduler enabled state is ambiguous",
        "Core Task Scheduler XML is invalid",
        "Core process is active but listener owner is unverified",
    }
)


def _category(exc: Exception) -> str:
    """Sanitize even exception types/messages from untrusted child code."""
    from serverfs_mcp.windows_core_control import CoreControlError

    if isinstance(exc, CoreControlError):
        return str(exc) if str(exc) in _ALLOWED_REASONS else "CoreControlError:other"
    return "other:" + (
        type(exc).__name__
        if type(exc).__name__ in {"OSError", "TimeoutExpired", "ValueError", "RuntimeError"}
        else "unexpected"
    )


def _operation(args: tuple[str, ...]) -> str:
    if not args:
        return "unrecognized"
    if args[0] == "/Query":
        return "query"
    if args[0] == "/Run":
        return "run"
    if args[0] == "/End":
        return "end"
    if args[0] == "/Change":
        if len(args) >= 4 and args[3] == "/ENABLE":
            return "change-enable"
        if len(args) >= 4 and args[3] == "/DISABLE":
            return "change-disable"
    return "unrecognized"


def _safe_status(snapshot: dict[str, Any]) -> dict[str, bool | str]:
    """Whitelisted non-private result fields needed by the existing W2i gate."""
    result: dict[str, bool | str] = {}
    for key in ("registered", "task_valid", "core_alive", "enabled"):
        value = snapshot.get(key)
        if type(value) is not bool:
            raise ValueError("nonbool Core status field")
        result[key] = value
    listener = snapshot.get("listener_owner")
    if listener not in ("verified", "not_verified"):
        raise ValueError("unknown Core listener status")
    result["listener_owner"] = listener
    return result


def _require_dedicated_runtime() -> None:
    from serverfs_mcp.native_lifecycle import window_local_app_data

    expected = window_local_app_data() / "ServerFS" / "core-owner-env"
    if (
        Path(sys.prefix).resolve(strict=True) != expected.resolve(strict=True)
        or not sys.flags.isolated
        or not sys.dont_write_bytecode
        or Path(sys.executable).resolve(strict=True)
        != (expected / "Scripts" / "python.exe").resolve(strict=True)
    ):
        raise ValueError("private Core runner provenance unavailable")


def _run_once(action: str) -> dict[str, Any]:
    if action not in ("start", "stop"):
        raise ValueError("unsupported Core lifecycle trace action")
    from serverfs_mcp import windows_core_control as control

    _require_dedicated_runtime()

    events: list[str] = []
    original_call: Callable[..., Any] = control._task_call
    original_owned: Callable[..., Any] = control._require_owned
    original_wait: Callable[..., Any] = control._wait_for_core
    original_status: Callable[..., Any] = control.status

    def add(label: str) -> None:
        if len(label) > 120:
            return
        if len(events) >= _MAX_EVENTS:
            # Retain the first ten launch facts and the most recent states,
            # including any late rollback failure after polling timeouts.
            events.pop(10)
        events.append(label)

    def traced_call(*args: str) -> Any:
        operation = _operation(args)
        if operation not in _ALLOWED_OPS:
            add("taskcall:unrecognized")
            # Pass through unchanged; never supply or modify arguments.
            return original_call(*args)
        try:
            answer = original_call(*args)
        except Exception as exc:
            add("taskcall:" + operation + ":raised:" + _category(exc))
            raise
        code = getattr(answer, "returncode", None)
        if type(code) is int:
            # Do not print arguments, raw process output or private paths.
            add("taskcall:" + operation + ":rc=" + str(code & 0xFFFFFFFF))
        else:
            add("taskcall:" + operation + ":unverified-result")
        return answer

    def traced_owned() -> Any:
        try:
            value = original_owned()
        except Exception as exc:
            add("owner:refused:" + _category(exc))
            raise
        add("owner:verified")
        return value

    def traced_wait(*, alive: bool, timeout_seconds: float = 20.0) -> Any:
        add("wait:" + ("alive" if alive else "offline") + ":start")
        try:
            result = original_wait(alive=alive, timeout_seconds=timeout_seconds)
        except Exception as exc:
            add("wait:refused:" + _category(exc))
            raise
        add("wait:verified")
        return result

    def traced_status() -> Any:
        try:
            value = original_status()
        except Exception as exc:
            add("status:refused:" + _category(exc))
            raise
        if isinstance(value, dict):
            add(
                "status:task="
                + ("verified" if value.get("task_valid") is True else "unverified")
                + ":owner="
                + ("live" if value.get("core_alive") is True else "offline")
            )
        else:
            add("status:unverified")
        return value

    control._task_call = traced_call
    control._require_owned = traced_owned
    control._wait_for_core = traced_wait
    control.status = traced_status
    try:
        add(action + ":entered")
        try:
            result = control.start() if action == "start" else control.stop()
            status = _safe_status(result)
            add(action + ":returned")
            return {"ok": True, "status": status, "trace": events, "reason": "none"}
        except Exception as exc:
            add(action + ":failed:" + _category(exc))
            return {"ok": False, "trace": events, "reason": _category(exc)}
    finally:
        # In-memory-only restoration. The installed Wheel is never edited.
        control._task_call = original_call
        control._require_owned = original_owned
        control._wait_for_core = original_wait
        control.status = original_status


def main(argv: list[str] | None = None) -> int:
    if os.name != "nt" or sys.platform != "win32":
        print("W2P_CORE=WINDOWS_REQUIRED", file=sys.stderr)
        return 2
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ("start", "stop"):
        print("W2P_CORE=INVALID_ACTION", file=sys.stderr)
        return 2
    try:
        output = _run_once(argv[0])
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0 if output["ok"] else 2
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "trace": [], "reason": _category(exc)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
