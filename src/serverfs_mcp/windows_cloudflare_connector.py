"""Cloudflare connector-only Windows generation for independent Core (v0.25.0 W1d).

Unlike the old parallel runtime this NEVER starts, owns or monitors an HTTP
Origin/Core or Agent Bridge. The actual Core owner must be authenticated and
bound to the specific Task Scheduler generation BEFORE the connector starts.
This module is source-only and is not wired to the legacy production task.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from .windows_cloudflare_runtime import (
    _launch,
    _minimal_windows_environment,
    _probe_unauthenticated_origin,
)


class ConnectorOnlyError(Exception):
    """Redacted connector-only failure."""


def run_connector_generation(
    *,
    cloudflared: Path,
    token_file: Path,
    core_port: int,
    core_hostname: str,
    job: Any,
    stdout: BinaryIO,
    stderr: BinaryIO,
    verify_core_owner: Callable[[], bool] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    probe: Callable[[int, str], bool] = _probe_unauthenticated_origin,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    check_interval_seconds: float = 5.0,
) -> int:
    """Run one Cloudflare process only, within the caller's connector Job.

    The Core owner verifier is required and must check Task Scheduler identity,
    PID/creation-time and listener ownership independently. A bare HTTP 401 is
    necessary but NOT sufficient because the legacy Origin might answer it.
    """
    if sys.platform != "win32":
        raise ConnectorOnlyError("Cloudflare connector-only generation requires Windows")
    if type(core_port) is not int or not 1 <= core_port <= 65535:
        raise ConnectorOnlyError("connector target Core port is invalid")
    if type(check_interval_seconds) not in (int, float) or not 0 < check_interval_seconds <= 60:
        raise ConnectorOnlyError("connector owner check interval is invalid")
    from .windows_core_service import WindowsCoreError, _strict_hostname

    try:
        _strict_hostname(core_hostname)
    except WindowsCoreError as exc:
        raise ConnectorOnlyError("connector Core hostname is invalid") from exc
    for path in (cloudflared, token_file):
        if not path.is_absolute() or not path.is_file():
            raise ConnectorOnlyError("connector private runtime inputs are unavailable")
    if verify_core_owner is None:
        # Production default: call the registered Task + process creation
        # time + Win32 TCP listener proof; never trust a 401 alone.
        from .windows_core_listener import prove_core_listener

        def verify_core_owner() -> bool:
            prove_core_listener(
                expected_port=core_port,
                expected_hostname=core_hostname,
            )
            return True

    try:
        if not verify_core_owner():
            raise ConnectorOnlyError("independent Core process ownership is not verified")
        if not probe(core_port, core_hostname):
            raise ConnectorOnlyError("independent Core refused its unauthenticated HTTP check")
        process = _launch(
            cloudflared,
            ["tunnel", "--no-autoupdate", "run", "--token-file", str(token_file)],
            env=_minimal_windows_environment(),
            cwd=cloudflared.parent,
            job=job,
            stdout=stdout,
            stderr=stderr,
            popen=popen,
        )
        next_check = clock() + check_interval_seconds
        while process.poll() is None:
            sleep(0.2)
            now = clock()
            if now >= next_check:
                if not verify_core_owner():
                    raise ConnectorOnlyError("independent Core owner changed")
                next_check = now + check_interval_seconds
        return process.wait() or 2
    except ConnectorOnlyError:
        raise
    except Exception as exc:  # noqa: BLE001 - no source paths in error text
        raise ConnectorOnlyError(
            f"Cloudflare connector generation failed ({type(exc).__name__})"
        ) from exc
