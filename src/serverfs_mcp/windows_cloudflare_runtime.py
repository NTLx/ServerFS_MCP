"""One bounded Cloudflare connector + loopback HTTP origin generation on Windows.

The existing Task Scheduler watchdog owns the runner. The runner owns ONE Job
Object containing both children; closing it terminates both. No Windows Service,
additional watchdog, background daemon or raw token command-line argument.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

START_TIMEOUT_SECONDS = 15.0
CHECK_INTERVAL_SECONDS = 0.2
CREATE_SUSPENDED = 0x00000004


class WindowsCloudflareRuntimeError(Exception):
    """Redacted operator-facing generation failure."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _minimal_windows_environment() -> dict[str, str]:
    keep = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in keep}


def _launch(
    executable: Path,
    args: list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    job: Any,
    stdout: BinaryIO,
    stderr: BinaryIO,
    popen: Callable[..., Any],
) -> Any:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | CREATE_SUSPENDED
    process = popen(
        [str(executable), *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=dict(env),
        creationflags=flags,
    )
    try:
        job.assign(process)
        job.resume(process)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    return process


def _probe_unauthenticated_origin(port: int, hostname: str) -> bool:
    # Do not consult user HTTP(S)_PROXY for a loopback health probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", headers={"Host": hostname})
        response = opener.open(request, timeout=1)
        response.close()
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except (OSError, urllib.error.URLError):
        return False
    return False  # A 2xx response to a request without credentials is unsafe.


def run_cloudflare_generation(
    *,
    python_executable: Path,
    config_path: Path,
    cloudflared_executable: Path,
    token_file: Path,
    http_port: int,
    origin_environment: Mapping[str, str],
    job: Any,
    origin_stdout: BinaryIO,
    origin_stderr: BinaryIO,
    connector_stdout: BinaryIO,
    connector_stderr: BinaryIO,
    popen: Callable[..., Any] = subprocess.Popen,
    on_child_started: Callable[[str, Any], None] | None = None,
    probe: Callable[[int, str], bool] = _probe_unauthenticated_origin,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run a single contained generation; return nonzero to request restart.

    The caller opens/closes its kill-on-close Job Object. The connector is
    never started until HTTP origin responds 401 to an anonymous request.
    """
    if not _is_windows():
        raise WindowsCloudflareRuntimeError("Cloudflare native runner requires Windows")
    if not 1 <= http_port <= 65535:
        raise WindowsCloudflareRuntimeError("invalid loopback port")
    for path in (python_executable, config_path, cloudflared_executable, token_file):
        if not path.is_absolute() or not path.is_file():
            raise WindowsCloudflareRuntimeError("Cloudflare runner input path is invalid")
    hostname = origin_environment.get("SERVERFS_HTTP_ALLOWED_HOSTS", "")
    if not hostname or "," in hostname or "://" in hostname:
        raise WindowsCloudflareRuntimeError("exact HTTP origin host is required")
    if not origin_environment.get("SERVERFS_HTTP_API_KEY") and (
        origin_environment.get("SERVERFS_OAUTH_ENABLED", "").lower() != "true"
    ):
        raise WindowsCloudflareRuntimeError("HTTP origin requires API Key or OAuth")

    base_environment = _minimal_windows_environment()
    origin_env = {**base_environment, **origin_environment}
    cwd = config_path.parent
    try:
        origin = _launch(
            python_executable,
            [
                "-m",
                "serverfs_mcp.cli",
                "serve",
                "--config",
                str(config_path),
                "--transport",
                "streamable-http",
                "--http-port",
                str(http_port),
            ],
            env=origin_env,
            cwd=cwd,
            job=job,
            stdout=origin_stdout,
            stderr=origin_stderr,
            popen=popen,
        )
        if on_child_started is not None:
            on_child_started("origin", origin)
        deadline = clock() + START_TIMEOUT_SECONDS
        while clock() < deadline:
            if origin.poll() is not None:
                raise WindowsCloudflareRuntimeError("HTTP origin exited before readiness")
            if probe(http_port, hostname):
                break
            sleep(CHECK_INTERVAL_SECONDS)
        else:
            raise WindowsCloudflareRuntimeError("HTTP origin readiness check timed out")

        connector = _launch(
            cloudflared_executable,
            ["tunnel", "--no-autoupdate", "run", "--token-file", str(token_file)],
            env=base_environment,
            cwd=cwd,
            job=job,
            stdout=connector_stdout,
            stderr=connector_stderr,
            popen=popen,
        )
        if on_child_started is not None:
            on_child_started("connector", connector)
        # One failure tears down the whole generation via the caller's Job close.
        while origin.poll() is None and connector.poll() is None:
            sleep(CHECK_INTERVAL_SECONDS)
        return 2
    except WindowsCloudflareRuntimeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowsCloudflareRuntimeError(
            f"Cloudflare process generation could not start ({type(exc).__name__})"
        ) from exc
