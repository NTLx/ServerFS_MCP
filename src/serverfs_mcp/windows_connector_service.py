"""Independent Windows OpenAI / Cloudflare connector process owner (v0.25.0 W1f).

One Task Scheduler-owned Python process -> one anonymous kill-on-close Job ->
one connector binary. Never owns or starts HTTP Core or Agent Bridge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import windows_connector_task_spec as task_spec


class ConnectorServiceError(Exception):
    """Redacted component-only refusal."""


def _check_windows() -> None:
    if sys.platform != "win32" or os.name != "nt":
        raise ConnectorServiceError("connector owner requires native Windows")


def service_home(kind: str) -> Path:
    from .native_lifecycle import window_local_app_data

    if kind not in task_spec.TASK_NAMES:
        raise ConnectorServiceError("connector component kind is invalid")
    return (
        window_local_app_data() / "ServerFS" / "service-manager" / "windows" / "connectors" / kind
    )


def policy_path(kind: str) -> Path:
    return service_home(kind) / "policy.json"


def manifest_path(kind: str) -> Path:
    return service_home(kind) / "service.json"


def task_path(kind: str) -> Path:
    return service_home(kind) / "task.xml"


def state_path(kind: str) -> Path:
    return service_home(kind) / "state.json"


def _check_private(path: Path, *, directory: bool) -> None:
    from .windows_core_control import _check_private as check_private
    from .windows_core_control import _current_sid

    try:
        check_private(path, directory=directory, sid=_current_sid())
    except Exception as exc:  # noqa: BLE001 - do not leak private filesystem paths
        raise ConnectorServiceError("connector private owner or DACL is invalid") from exc


def load_policy(kind: str) -> dict[str, Any]:
    """No embedded key values: policy consists only of trusted paths/settings."""
    _check_windows()
    path = policy_path(kind)
    _check_private(path, directory=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorServiceError("connector policy cannot be parsed") from exc
    common = {"schema_version", "kind", "core_port", "core_hostname"}
    if kind == "cloudflare":
        allowed = common | {"cloudflared", "token_file"}
    else:
        allowed = common | {
            "tunnel_client",
            "tunnel_id",
            "control_plane_key_file",
            "core_auth_header_file",
        }
    if (
        not isinstance(data, dict)
        or set(data) != allowed
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("kind") != kind
        or type(data.get("core_port")) is not int
        or not 1 <= data["core_port"] <= 65535
    ):
        raise ConnectorServiceError("connector policy schema is invalid")
    from .windows_core_service import WindowsCoreError, _strict_hostname

    try:
        _strict_hostname(data["core_hostname"])
    except (WindowsCoreError, TypeError) as exc:
        raise ConnectorServiceError("connector Core hostname is invalid") from exc
    expected_paths = (
        ("cloudflared", "token_file")
        if kind == "cloudflare"
        else ("tunnel_client", "control_plane_key_file", "core_auth_header_file")
    )
    for key in expected_paths:
        if not isinstance(data[key], str) or not Path(data[key]).is_absolute():
            raise ConnectorServiceError("connector runtime path is invalid")
    if kind == "openai":
        from .windows_openai_connector import OpenAIHTTPConnector

        try:
            OpenAIHTTPConnector(
                tunnel_client=Path(data["tunnel_client"]),
                tunnel_id=data["tunnel_id"],
                control_plane_key_file=Path(data["control_plane_key_file"]),
                core_auth_header_file=Path(data["core_auth_header_file"]),
                core_port=data["core_port"],
                core_hostname=data["core_hostname"],
            ).validated()
        except (ValueError, TypeError) as exc:
            raise ConnectorServiceError("OpenAI connector policy is invalid") from exc
    elif Path(data["cloudflared"]).name.casefold() != "cloudflared.exe":
        raise ConnectorServiceError("Cloudflare connector executable is invalid")
    from .windows_core_control import _require_owned as core_owner

    try:
        core_manifest, _xml = core_owner()
    except Exception as exc:  # noqa: BLE001 - unknown Core is not a safe target
        raise ConnectorServiceError("independent Core registration is not verified") from exc
    if (
        core_manifest["port"] != data["core_port"]
        or core_manifest["hostname"] != data["core_hostname"]
    ):
        raise ConnectorServiceError("connector Core policy does not match registration")
    return data


def validate_runtime_inputs(kind: str, data: dict[str, Any]) -> None:
    """Verify every executable/credential path is outside exposed workdirs."""
    from .windows_cloudflare_security import validate_private_file
    from .windows_core_control import _current_sid
    from .windows_core_control import _require_owned as core_owner
    from .windows_core_service import _verified_config

    manifest, _xml = core_owner()
    _config, roots = _verified_config(Path(manifest["config_path"]))
    from .windows_cloudflare_security import _within

    home = service_home(kind).resolve(strict=True)
    if any(_within(home, root) or _within(root, home) for root in roots):
        raise ConnectorServiceError("connector private home overlaps an exposed workdir")
    checkout = Path(__file__).resolve().parents[2]
    if kind == "cloudflare":
        inputs = ("cloudflared", "token_file")
    else:
        inputs = ("tunnel_client", "control_plane_key_file", "core_auth_header_file")
    for key in inputs:
        try:
            validate_private_file(
                Path(data[key]),
                checkout_root=checkout,
                workdir_roots=roots,
                user_sid=_current_sid(),
            )
        except Exception as exc:  # noqa: BLE001 - no credential metadata in error
            raise ConnectorServiceError("connector private runtime input is unsafe") from exc


@contextmanager
def connector_mutex(kind: str):
    """Separate SID/kind owner; unlike the Core/Bridge mutex it owns no peer."""
    _check_windows()
    import ctypes

    from .windows_core_control import _current_sid

    sid = _current_sid()
    identity = hashlib.sha256(f"{kind}|{sid}".encode("ascii")).hexdigest()[:32]
    name = "Local\\serverfs-connector-" + identity
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ConnectorServiceError("connector owner mutex could not be created")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        raise ConnectorServiceError("another connector generation already owns this kind")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def run_owned_connector(kind: str, policy: Path) -> int:
    """Task-bound source runtime. Never launch a second Core/Agent Bridge."""
    _check_windows()
    if kind not in task_spec.TASK_NAMES:
        raise ConnectorServiceError("connector component kind is invalid")
    if policy != policy_path(kind):
        raise ConnectorServiceError("connector policy is outside its fixed task scope")
    from . import windows_connector_control as control

    with connector_mutex(kind):
        data = load_policy(kind)
        validate_runtime_inputs(kind, data)
        control.record_runtime_owner(kind)
        from .windows_job import WindowsJob

        home = service_home(kind)
        _check_private(home, directory=True)
        log_dir = home / "logs"
        _check_private(log_dir, directory=True)
        stdout_path = log_dir / "connector.stdout.log"
        stderr_path = log_dir / "connector.stderr.log"
        _check_private(stdout_path, directory=False)
        _check_private(stderr_path, directory=False)
        with (
            stdout_path.open("ab", buffering=0) as stdout,
            stderr_path.open("ab", buffering=0) as stderr,
            WindowsJob() as job,
        ):
            if kind == "cloudflare":
                from .windows_cloudflare_connector import run_connector_generation

                return run_connector_generation(
                    cloudflared=Path(data["cloudflared"]),
                    token_file=Path(data["token_file"]),
                    core_port=data["core_port"],
                    core_hostname=data["core_hostname"],
                    job=job,
                    stdout=stdout,
                    stderr=stderr,
                )
            from .windows_openai_connector import OpenAIHTTPConnector, run_connector_generation

            return run_connector_generation(
                OpenAIHTTPConnector(
                    tunnel_client=Path(data["tunnel_client"]),
                    tunnel_id=data["tunnel_id"],
                    control_plane_key_file=Path(data["control_plane_key_file"]),
                    core_auth_header_file=Path(data["core_auth_header_file"]),
                    core_port=data["core_port"],
                    core_hostname=data["core_hostname"],
                ),
                job=job,
                stdout=stdout,
                stderr=stderr,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serverfs-windows-connector")
    parser.add_argument("_mode", choices=("_run",))
    parser.add_argument("--kind", required=True, choices=tuple(task_spec.TASK_NAMES))
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run_owned_connector(args.kind, args.policy)
    except ConnectorServiceError as exc:
        print(f"serverfs-windows-connector: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - no internal paths or key values
        print(
            f"serverfs-windows-connector: failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
