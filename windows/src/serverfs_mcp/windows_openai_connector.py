"""OpenAI Tunnel HTTP-upstream connector-only command contract (v0.25.0 W1d).

The installed tunnel-client 0.0.17 advertises --mcp.server-url and accepts
file:-backed static HTTP header values. It must never launch a stdio Core,
Agent Bridge, or supervisor in this mode. This module builds arguments only;
no Tunnel, process, service or Task Scheduler action occurs on import.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .windows_cloudflare_runtime import _launch, _probe_unauthenticated_origin
from .windows_core_service import WindowsCoreError, _strict_hostname

_TUNNEL_ID = re.compile(r"tunnel_[a-z0-9]{32}\Z")  # Frozen native_tunnel contract.


class OpenAIConnectorError(Exception):
    """Safe refusal without raw URLs, private paths or credentials."""


@dataclass(frozen=True)
class OpenAIHTTPConnector:
    tunnel_client: Path
    tunnel_id: str
    control_plane_key_file: Path
    core_auth_header_file: Path
    core_port: int
    core_hostname: str

    def validated(self) -> OpenAIHTTPConnector:
        if not isinstance(self.tunnel_id, str) or not _TUNNEL_ID.fullmatch(self.tunnel_id):
            raise OpenAIConnectorError("OpenAI connector Tunnel ID is invalid")
        if type(self.core_port) is not int or not 1 <= self.core_port <= 65535:
            raise OpenAIConnectorError("OpenAI connector Core port is invalid")
        try:
            _strict_hostname(self.core_hostname)
        except WindowsCoreError as exc:
            raise OpenAIConnectorError("OpenAI connector Core Host is invalid") from exc
        paths = (
            self.tunnel_client,
            self.control_plane_key_file,
            self.core_auth_header_file,
        )
        if not all(path.is_absolute() for path in paths):
            raise OpenAIConnectorError("OpenAI connector paths must be absolute")
        return self


def command(spec: OpenAIHTTPConnector) -> list[str]:
    """Only fixed flags; all secret values remain in external protected files.

    Core must allow the explicit local Host 127.0.0.1:port. The separate
    Core ownership verifier and private ACL checks are install-time gates.
    """
    spec.validated()
    origin = f"http://127.0.0.1:{spec.core_port}/mcp"
    return [
        str(spec.tunnel_client),
        "run",
        "--control-plane.tunnel-id",
        spec.tunnel_id,
        "--control-plane.api-key",
        f"file:{spec.control_plane_key_file}",
        "--mcp.server-url",
        f"url={origin}",
        "--mcp.extra-headers",
        f"Authorization: file:{spec.core_auth_header_file}",
    ]


def environment(source: Mapping[str, str]) -> dict[str, str]:
    """Drop ambient headers, provider secrets and egress proxy configuration."""
    allowed = frozenset(
        {
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
    )
    env = {k: v for k, v in source.items() if k.upper() in allowed}
    env["HEALTH_LISTEN_ADDR"] = "127.0.0.1:0"
    return env


def run_connector_generation(
    spec: OpenAIHTTPConnector,
    *,
    job: Any,
    stdout: BinaryIO,
    stderr: BinaryIO,
    verify_core_owner: Callable[[], bool] | None = None,
    probe: Callable[[int, str], bool] = _probe_unauthenticated_origin,
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    check_interval_seconds: float = 5.0,
) -> int:
    """Spawn exactly one contained OpenAI HTTP connector, never a Core/Bridge.

    The separate caller owns only this connector's kill-on-close Job, and
    closes it on every normal/failure return. Check Core ownership both before
    and during the session; the Core's disappearance must not keep a stale
    Tunnel connected to an unrelated 401 responder.
    """
    import sys

    if sys.platform != "win32":
        raise OpenAIConnectorError("OpenAI connector-only generation requires Windows")
    spec.validated()
    if type(check_interval_seconds) is not float and type(check_interval_seconds) is not int:
        raise OpenAIConnectorError("connector owner check interval is invalid")
    if not 0 < check_interval_seconds <= 60:
        raise OpenAIConnectorError("connector owner check interval is invalid")
    for path in (
        spec.tunnel_client,
        spec.control_plane_key_file,
        spec.core_auth_header_file,
    ):
        if not path.is_file():
            raise OpenAIConnectorError("connector runtime input is unavailable")
    if verify_core_owner is None:
        from .windows_core_listener import prove_core_listener

        def verify_core_owner() -> bool:
            prove_core_listener(
                expected_port=spec.core_port,
                expected_hostname=spec.core_hostname,
            )
            return True

    try:
        if not verify_core_owner():
            raise OpenAIConnectorError("independent Core owner is not verified")
        # OpenAI connects with loopback Host, unlike Cloudflare public Host.
        if not probe(spec.core_port, f"127.0.0.1:{spec.core_port}"):
            raise OpenAIConnectorError("independent Core is not authenticated")
        argv = command(spec)
        process = _launch(
            spec.tunnel_client,
            argv[1:],
            env=environment(os.environ),
            cwd=spec.tunnel_client.parent,
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
                    raise OpenAIConnectorError("independent Core owner changed")
                next_check = now + check_interval_seconds
        return process.wait() or 2
    except OpenAIConnectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - no credentials or private paths in errors
        raise OpenAIConnectorError(
            f"OpenAI connector-only generation failed ({type(exc).__name__})"
        ) from exc
