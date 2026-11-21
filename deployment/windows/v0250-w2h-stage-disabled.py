"""W2h current-user disabled-only independent component staging.

This is not a release/cutover tool. Requires an already accepted W2g host
report and a PASS from v0250-w2h-host-preflight.ps1. Starts no services,
does not terminate providers, and never mutates either legacy Task Scheduler
identity. Every new task must remain DISABLED, including after failure.
Writes only new, exact private service-home artifacts and one redacted report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_HOSTNAME = "workpc-serverfs.ntlx.top"
_CORE_PORT = 18766
_TUNNEL_SHA = "61a770c759c386845f830a106f7e6d0d3f854b932259744a88b4013c282e64ec"
_TUNNEL_ID = re.compile(r"tunnel_[a-z0-9]{32}\Z")
_ENV_NAMES = (
    "bridge-owner-env",
    "bridge-runtime-env",
    "core-owner-env",
    "cloudflare-connector-env",
    "openai-connector-env",
)
_TASKS = (
    ("core", "core-owner-env", "ServerFS MCP Core"),
    ("bridge", "bridge-owner-env", "ServerFS Agent Bridge"),
    ("cloudflare", "cloudflare-connector-env", "ServerFS Cloudflare Connector"),
    ("openai", "openai-connector-env", "ServerFS OpenAI Connector"),
)


class W2hStagingError(Exception):
    """Fail-closed partial-install diagnostic without user paths or keys."""


def _sha256_file(path: Path) -> str:
    hash_value = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            hash_value.update(part)
    return hash_value.hexdigest()


def _new_directory(path: Path, sid: str, protect) -> None:
    if path.exists() or path.is_symlink():
        raise W2hStagingError("private service directory already exists")
    if not path.parent.is_dir():
        raise W2hStagingError("private parent does not exist")
    path.mkdir()
    protect.protect_private_directory(path, sid)
    from serverfs_mcp.windows_core_control import _check_private

    _check_private(path, directory=True, sid=sid)


def _new_private_file(path: Path, value: bytes, sid: str) -> None:
    from serverfs_mcp.windows_core_control import _check_private
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    if path.exists() or path.is_symlink():
        raise W2hStagingError("private file already exists")
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    protect_new_file(path, sid)
    _check_private(path, directory=False, sid=sid)


def _copy_private(source: Path, dest: Path, sid: str, *, expected_hash: str | None) -> None:
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    if not source.is_absolute() or not source.is_file():
        raise W2hStagingError("trusted copy source is unavailable")
    _no_reparse_components(source)
    if source.is_symlink() or dest.exists() or dest.is_symlink():
        raise W2hStagingError("private copy source/destination is redirected or occupied")
    if expected_hash and _sha256_file(source) != expected_hash.lower():
        raise W2hStagingError("source executable differs from pinned version")
    # Copy, not hardlink: updating new ACL must never alter legacy image.
    with source.open("rb") as inbound, dest.open("xb") as outbound:
        shutil.copyfileobj(inbound, outbound)
        outbound.flush()
        os.fsync(outbound.fileno())
    from serverfs_mcp.windows_core_control import _check_private
    from serverfs_mcp.windows_private_artifacts import protect_new_file

    protect_new_file(dest, sid)
    _check_private(dest, directory=False, sid=sid)
    if _sha256_file(source) != _sha256_file(dest):
        raise W2hStagingError("private executable or key copy differs from source")
    if expected_hash and _sha256_file(dest) != expected_hash.lower():
        raise W2hStagingError("private executable digest differs from accepted version")


def _cli_component(python: Path, component: str, args: list[str]) -> dict[str, object]:
    proc = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "serverfs_mcp.windows_component_cli",
            component,
            *args,
        ],
        cwd=python.parent,
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "TEMP",
                "TMP",
                "APPDATA",
                "LOCALAPPDATA",
                "USERPROFILE",
                "PROGRAMDATA",
            }
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=75,
    )
    if proc.returncode != 0:
        raise W2hStagingError("component installer refused a disabled registration")
    try:
        state = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise W2hStagingError("component installer returned invalid status") from exc
    if not isinstance(state, dict) or not (
        state.get("registered") is True
        and state.get("task_valid") is True
        and state.get("enabled") is False
    ):
        raise W2hStagingError("component task did not attest disabled ownership")
    return state


def _private_homes_and_config() -> tuple[Path, dict[str, Path], str, dict, dict]:
    from serverfs_mcp import windows_cloudflare_parallel, windows_service
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.native_lifecycle import window_local_app_data
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components

    base = window_local_app_data() / "ServerFS"
    sid = current_user_sid()
    default = windows_service._manifest_document()
    cloudflare = windows_cloudflare_parallel._manifest_document()
    if (
        default.get("user_sid") != sid
        or cloudflare.get("user_sid") != sid
        or default.get("task_name") != "ServerFS Native Tunnel"
        or cloudflare.get("task_name") != "ServerFS Cloudflare WorkPC"
    ):
        raise W2hStagingError("legacy manifest owner or task identity is unverified")
    config = Path(default["config_path"]).resolve(strict=True)
    if config != Path(cloudflare["config_path"]).resolve(strict=True):
        raise W2hStagingError("production native policies do not match")
    _no_reparse_components(config)
    homes = {
        "core": base / "service-manager" / "windows" / "mcp-core",
        "bridge": base / "service-manager" / "windows" / "agent-bridge",
        "cloudflare": base / "service-manager" / "windows" / "connectors" / "cloudflare",
        "openai": base / "service-manager" / "windows" / "connectors" / "openai",
    }
    return config, homes, sid, default, cloudflare


def _stage(events: list[str]) -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise W2hStagingError("native current-user Windows is required")

    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    import importlib.util

    from serverfs_mcp import windows_service
    from serverfs_mcp.cloudflare_bootstrap import WINDOWS_BINARY_SHA256
    from serverfs_mcp.native_lifecycle import window_local_app_data
    from serverfs_mcp.windows_cloudflare_config import _port_available
    from serverfs_mcp.windows_cloudflare_security import (
        _no_reparse_components,
        _read_windows_file_sddl,
        _sddl_sections,
        validate_private_file,
    )
    from serverfs_mcp.windows_core_control import _check_private
    from serverfs_mcp.windows_core_service import _verified_config

    helper = repo / "deployment" / "windows" / "v0250-w2g-private-acl.py"
    spec = importlib.util.spec_from_file_location("w2h_private_acl", helper)
    if spec is None or spec.loader is None:
        raise W2hStagingError("trusted private DACL helper unavailable")
    protect = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protect)

    config, homes, sid, default, cloudflare = _private_homes_and_config()
    _config, workdir_roots = _verified_config(config)
    from serverfs_mcp.windows_cloudflare_security import _within

    for home in homes.values():
        if any(_within(home, root) or _within(root, home) for root in workdir_roots):
            raise W2hStagingError("private component home overlaps an exposed workdir")
        if home.exists() or home.is_symlink():
            raise W2hStagingError("independent service home already exists")
    for label in _ENV_NAMES:
        prefix = window_local_app_data() / "ServerFS" / label
        for item in (prefix, prefix / "Scripts", prefix / "Lib", prefix / "Lib/site-packages"):
            _check_private(item, directory=True, sid=sid)
        _check_private(prefix / "Scripts" / "pythonw.exe", directory=False, sid=sid)
    if not _port_available(_CORE_PORT):
        raise W2hStagingError("independent Core loopback port is in use")
    if cloudflare.get("hostname") != _HOSTNAME:
        raise W2hStagingError("legacy Cloudflare Host identity differs")
    cloudflare_exe = Path(cloudflare["cloudflared"])
    cloudflare_token = Path(cloudflare["token_file"])
    validate_private_file(
        cloudflare_token, checkout_root=repo, workdir_roots=workdir_roots, user_sid=sid
    )
    if not 1 <= cloudflare_token.stat().st_size <= 16384:
        raise W2hStagingError("existing Cloudflare token file size is invalid")
    _no_reparse_components(cloudflare_exe)
    if (
        not cloudflare_exe.is_file()
        or _sha256_file(cloudflare_exe) != WINDOWS_BINARY_SHA256.lower()
    ):
        raise W2hStagingError("existing Cloudflare executable no longer matches pin")

    # Only take the existing tunnel ID from the validated manifest's env
    # reference; never publish or copy the repo-root .env into a new service.
    legacy_env = Path(default["env_file"])
    if legacy_env.resolve(strict=True) != (repo / ".env").resolve(strict=True):
        raise W2hStagingError("legacy env file reference differs from audited repository")
    values = windows_service._read_env_file(legacy_env)
    tunnel_id = values.get("CONTROL_PLANE_TUNNEL_ID", "").strip()
    if not _TUNNEL_ID.fullmatch(tunnel_id):
        raise W2hStagingError("legacy OpenAI Tunnel identifier is unverified")

    source_key = windows_service.default_api_key_path()
    if not source_key.is_file():
        raise W2hStagingError("existing OpenAI control-plane key source is unavailable")
    if not 1 <= source_key.stat().st_size <= 16384:
        raise W2hStagingError("legacy control-plane key file size is invalid")
    _no_reparse_components(source_key)
    # Legacy API key's inherited DACL is a known W0 deviation. It may be
    # copied to a protected new file ONLY if ownership is the same SID.
    # Do not change the ACL of the legacy key in place.
    if _sddl_sections(_read_windows_file_sddl(source_key)).get("O") != sid:
        raise W2hStagingError("legacy control-plane key owner is unverified")
    if any(_within(source_key, root) for root in workdir_roots):
        raise W2hStagingError("legacy control-plane key overlaps an exposed workdir")
    source_tunnel = (
        window_local_app_data()
        / "ServerFS"
        / "bin"
        / "tunnel-client-v0.0.17-windows-amd64"
        / "tunnel-client.exe"
    )
    _no_reparse_components(source_tunnel)
    if not source_tunnel.is_file() or _sha256_file(source_tunnel) != _TUNNEL_SHA:
        raise W2hStagingError("trusted OpenAI Tunnel executable digest differs")

    # All four names must be absent at the authoritative PowerShell gate.
    # This exact-source Python call additionally checks absence through
    # component-owned Scheduler APIs, never assumes an AccessDenied is absent.
    from serverfs_mcp import (
        windows_bridge_control,
        windows_connector_control,
        windows_core_control,
    )

    if windows_core_control._registered_xml(optional=True) is not None:
        raise W2hStagingError("Core Task Scheduler identity already exists")
    if windows_bridge_control._registered_xml(optional=True) is not None:
        raise W2hStagingError("Bridge Task Scheduler identity already exists")
    for kind in ("cloudflare", "openai"):
        if windows_connector_control._registered(kind, optional=True) is not None:
            raise W2hStagingError("connector Task Scheduler identity already exists")

    events.append("fixed-input-and-component-name-preflight: PASS")
    # Never mutate the pre-existing parent directories or production tasks.
    shared = homes["core"].parent
    _no_reparse_components(shared)
    if not shared.is_dir():
        raise W2hStagingError("existing Windows service manager parent is missing")
    connectors_parent = homes["cloudflare"].parent
    if connectors_parent.exists() or connectors_parent.is_symlink():
        raise W2hStagingError("connectors parent already exists; refusing overwrite")

    _new_directory(homes["core"], sid, protect)
    _new_directory(homes["bridge"], sid, protect)
    _new_directory(connectors_parent, sid, protect)
    for kind in ("cloudflare", "openai"):
        _new_directory(homes[kind], sid, protect)
    events.append("four-private-service-homes: PASS")

    logs = {
        "bridge": ("lifecycle.log", "runner.stderr.log"),
        "cloudflare": ("connector.stdout.log", "connector.stderr.log"),
        "openai": ("connector.stdout.log", "connector.stderr.log"),
    }
    for kind, names in logs.items():
        folder = homes[kind] / "logs"
        _new_directory(folder, sid, protect)
        for name in names:
            _new_private_file(folder / name, b"", sid)
    events.append("private-service-log-files: PASS")

    core_key = secrets.token_urlsafe(48)
    origin_file = homes["core"] / "http-origin.env"
    _new_private_file(
        origin_file,
        (
            f"SERVERFS_HTTP_API_KEY={core_key}\n"
            f"SERVERFS_HTTP_ALLOWED_HOSTS={_HOSTNAME},127.0.0.1:{_CORE_PORT}\n"
        ).encode("ascii"),
        sid,
    )
    events.append("private-new-Core-auth-file: PASS")

    # Register the independent Core first; connector policy validates its
    # registered identity.  The CLI task XML is DISABLED by construction.
    core_python = window_local_app_data() / "ServerFS" / "core-owner-env" / "Scripts" / "python.exe"
    _cli_component(
        core_python,
        "core",
        [
            "install",
            "--config",
            str(config),
            "--auth-file",
            str(origin_file),
            "--port",
            str(_CORE_PORT),
            "--hostname",
            _HOSTNAME,
        ],
    )
    events.append("Core-disabled-registration: PASS")

    bridge_python = (
        window_local_app_data() / "ServerFS" / "bridge-owner-env" / "Scripts" / "python.exe"
    )
    _cli_component(bridge_python, "bridge", ["install", "--config", str(config)])
    events.append("Bridge-disabled-registration: PASS")

    # Copy a pinned executable into an isolated protected private directory,
    # never change DACL on the legacy Cloudflare executable.
    cf_copy = homes["cloudflare"] / "cloudflared.exe"
    _copy_private(cloudflare_exe, cf_copy, sid, expected_hash=WINDOWS_BINARY_SHA256)
    cf_policy = {
        "schema_version": 1,
        "kind": "cloudflare",
        "core_port": _CORE_PORT,
        "core_hostname": _HOSTNAME,
        "cloudflared": str(cf_copy),
        "token_file": str(cloudflare_token),
    }
    _new_private_file(
        homes["cloudflare"] / "policy.json",
        json.dumps(cf_policy, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        sid,
    )
    cf_python = (
        window_local_app_data() / "ServerFS" / "cloudflare-connector-env" / "Scripts" / "python.exe"
    )
    _cli_component(cf_python, "cloudflare", ["install"])
    events.append("Cloudflare-disabled-registration: PASS")

    # Private copy, never source file DACL alteration, for a legacy
    # inherited-DACL control-plane key.  The new HTTP auth key is *different*.
    openai = homes["openai"]
    copied_client = openai / "tunnel-client.exe"
    copied_key = openai / "control-plane-key"
    core_header = openai / "core-authorization-header"
    _copy_private(source_tunnel, copied_client, sid, expected_hash=_TUNNEL_SHA)
    _copy_private(source_key, copied_key, sid, expected_hash=None)
    _new_private_file(core_header, f"Bearer {core_key}".encode("ascii"), sid)
    openai_policy = {
        "schema_version": 1,
        "kind": "openai",
        "core_port": _CORE_PORT,
        "core_hostname": _HOSTNAME,
        "tunnel_client": str(copied_client),
        "tunnel_id": tunnel_id,
        "control_plane_key_file": str(copied_key),
        "core_auth_header_file": str(core_header),
    }
    _new_private_file(
        openai / "policy.json",
        json.dumps(openai_policy, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        sid,
    )
    oa_python = (
        window_local_app_data() / "ServerFS" / "openai-connector-env" / "Scripts" / "python.exe"
    )
    _cli_component(oa_python, "openai", ["install"])
    events.append("OpenAI-disabled-registration: PASS")

    # Independent status check of each exact new component via its own
    # dedicated installed interpreter; zero activation permitted.
    for kind, env_name, _task_name in _TASKS:
        installed = window_local_app_data() / "ServerFS" / env_name / "Scripts" / "python.exe"
        _cli_component(installed, kind, ["status"])
    events.append("all-four-disabled-component-statuses: PASS")


def main() -> int:
    if os.name != "nt" or sys.platform != "win32":
        print("W2H_STAGE=WINDOWS_REQUIRED", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parents[2]
    report = (
        repo
        / "docs"
        / "diagnostics"
        / (
            "v0250-windows-w2h-disabled-stage-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            + ".md"
        )
    )
    passed = False
    events: list[str] = []
    try:
        _stage(events)
        passed = True
    except Exception as exc:
        # Do not emit any path, ID, SID, auth header, secret, task XML or
        # subprocess output; a partially staged candidate is preserved.
        events.append(
            f"W2h partial stage: FAILED ({type(exc).__name__}; preserved without cleanup)"
        )
    lines = [
        "# Windows v0.25 W2h — disabled component stage",
        "",
        "Overall: " + ("PASS" if passed else "NOT_ACCEPTED"),
        "UTC: " + datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "",
        "## Non-secret gate outcomes",
        *("- " + event for event in events),
        "",
        "All four new tasks must remain Disabled; no component is started.",
        "The two legacy services, providers, workdirs and tunnel credentials were not mutated.",
        "Legacy secret inputs were used only as read-only inputs to privately protected copies.",
        "New private Core credentials and copied keys are never printed or stored in this report.",
        "A later W2h authority check must independently confirm legacy Running/Enabled,",
        "new four registered/disabled, no new runtime processes and task XML ownership.",
        "Candidate Wheel version metadata remains 0.24.2; not a v0.25.0 release.",
    ]
    try:
        with report.open("x", encoding="utf-8", newline="\n") as target:
            target.write("\n".join(lines) + "\n")
    except OSError:
        print("W2H_STAGE=REPORT_WRITE_FAILED", file=sys.stderr)
        return 2
    print(f"W2h disabled-stage report: {report}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
