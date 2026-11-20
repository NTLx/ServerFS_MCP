"""Explicit component-only Windows v0.25 service operator interface.

No legacy "serverfs service" alias or Tunnel/Bridge cascade. This CLI does
not automatically create folders, credentials, Python environments or
workdir permissions. A normal current-user admin must prepare private paths
once before any manifest is published.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from serverfs_mcp.instance_identity import DEFAULT_INSTANCE_ID, InstanceIdentityError, valid_id

from . import storage
from .spec import COMPONENTS, Manifest, ManifestError
from .tasks import Scheduler, TaskError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="serverfs-windows-v25")
    result.add_argument("component", choices=tuple(COMPONENTS))
    result.add_argument("--instance", default=DEFAULT_INSTANCE_ID)
    result.add_argument("--connector-id")
    result.add_argument(
        "action",
        choices=(
            "prepare",
            "configure",
            "install",
            "status",
            "start",
            "stop",
            "restart",
            "uninstall",
        ),
    )
    result.add_argument("--config", type=Path)
    result.add_argument("--auth-file", type=Path)
    result.add_argument("--port", type=int)
    result.add_argument("--hostname")
    result.add_argument("--bridge-python", type=Path)
    result.add_argument("--cloudflared", type=Path)
    result.add_argument("--token-file", type=Path)
    result.add_argument("--tunnel-client", type=Path)
    result.add_argument("--tunnel-id")
    result.add_argument("--control-plane-key-file", type=Path)
    result.add_argument("--core-auth-header-file", type=Path)
    return result


_FLAGS = {
    "config": "config",
    "auth_file": "auth_file",
    "port": "port",
    "hostname": "hostname",
    "bridge_python": "bridge_python",
    "cloudflared": "cloudflared",
    "token_file": "token_file",
    "tunnel_client": "tunnel_client",
    "tunnel_id": "tunnel_id",
    "control_plane_key_file": "control_plane_key_file",
    "core_auth_header_file": "core_auth_header_file",
}


def _requested_settings(args: argparse.Namespace) -> dict[str, str | int]:
    supplied = {
        field: getattr(args, option)
        for option, field in _FLAGS.items()
        if getattr(args, option) is not None
    }
    return {
        key: str(value) if isinstance(value, Path) else value for key, value in supplied.items()
    }


def _preflight(manifest: Manifest) -> None:
    from .runtime import _core_configuration, _private_input, _roots

    if manifest.component == "core":
        _core_configuration(manifest)
        if manifest.instance_id != DEFAULT_INSTANCE_ID:
            from serverfs_mcp.native_config import load_native_config

            workdirs, policy = load_native_config(Path(str(manifest.settings["config"])))
            if policy.bridge_required:
                from .core_bridge_binding import load_native_core_bridge_binding

                load_native_core_bridge_binding(manifest, workdirs)
        return
    if manifest.component == "bridge":
        if manifest.instance_id != DEFAULT_INSTANCE_ID:
            from .bridge_v2_owner import load_native_bridge_plan

            load_native_bridge_plan(manifest)
            return
        from serverfs_mcp.native_config import load_native_config

        config, roots = _roots(Path(str(manifest.settings["config"])))
        _workdirs, settings = load_native_config(config)
        if not settings.bridge_required:
            raise ManifestError("Bridge must be enabled by native workdir policy")
        _private_input(Path(str(manifest.settings["bridge_python"])), roots, manifest.sid)
        return
    core = (
        storage.load("core")
        if manifest.instance_id == DEFAULT_INSTANCE_ID
        else storage.load("core", manifest.instance_id)
    )
    if (
        manifest.settings["port"] != core.settings["port"]
        or manifest.settings["hostname"] != core.settings["hostname"]
    ):
        raise ManifestError("connector target does not match the independent Core")
    _config, roots = _roots(Path(str(core.settings["config"])))
    if manifest.component == "cloudflare":
        for key in ("cloudflared", "token_file"):
            _private_input(Path(str(manifest.settings[key])), roots, manifest.sid)
    else:
        for key in ("tunnel_client", "control_plane_key_file", "core_auth_header_file"):
            _private_input(Path(str(manifest.settings[key])), roots, manifest.sid)


def _assert_instance_isolation(manifest: Manifest) -> None:
    if manifest.instance_id == DEFAULT_INSTANCE_ID:
        return
    if manifest.component == "core":
        from .isolation import assert_core_isolation

        assert_core_isolation(manifest, storage.core_inventory())
    elif manifest.component == "bridge":
        from .bridge_v2_owner import load_owned_bridge_workdir_claims
        from .workdir_ownership import assert_independent_bridge_roots

        proposed = load_owned_bridge_workdir_claims(manifest)
        existing = tuple(
            claim
            for peer in storage.component_inventory("bridge")
            if peer.instance_id != manifest.instance_id
            for claim in load_owned_bridge_workdir_claims(peer)
        )
        assert_independent_bridge_roots(proposed, existing)
    elif manifest.component in ("cloudflare", "openai"):
        from .isolation import assert_connector_isolation

        peers = (
            *storage.component_inventory("cloudflare"),
            *storage.component_inventory("openai"),
        )
        assert_connector_isolation(manifest, peers)


def configure(args: argparse.Namespace) -> dict[str, bool | str]:
    from serverfs_mcp.native_endpoint import current_user_sid

    options = _requested_settings(args)
    profile = {
        "schema": 1 if args.instance == DEFAULT_INSTANCE_ID else 2,
        "component": args.component,
        "sid": current_user_sid(),
        "pythonw": str(
            storage.pinned_pythonw(args.component)
            if args.instance == DEFAULT_INSTANCE_ID
            else storage.pinned_pythonw(args.component, args.instance)
        ),
        "settings": options,
    }
    if args.instance != DEFAULT_INSTANCE_ID:
        profile["instance_id"] = args.instance
        if args.connector_id is not None:
            profile["connector_id"] = args.connector_id
    manifest = Manifest.parse(profile)
    _preflight(manifest)
    _assert_instance_isolation(manifest)
    storage.stage(manifest)
    return {"component": manifest.component, "configured": True}


def _legacy_bridge_active() -> bool:
    """Refuse second same-SID Bridge while the old OpenAI owner is still active."""
    from .tasks import _run

    script = (
        "$ErrorActionPreference='Stop';"
        "$t=@(Get-ScheduledTask -TaskPath '\\' -TaskName 'ServerFS Native Tunnel');"
        "if($t.Count -ne 1){exit 17};"
        "[Console]::Out.Write($t[0].State.ToString())"
    )
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    if result.returncode != 0:
        raise TaskError("legacy same-user Bridge ownership cannot be excluded")
    state = result.stdout.decode("ascii", errors="replace").strip()
    if state not in ("Running", "Ready", "Disabled", "Queued"):
        raise TaskError("legacy Bridge owner state is unknown")
    return state in ("Running", "Queued")


def dispatch(args: argparse.Namespace) -> dict[str, bool | str]:
    try:
        valid_id(args.instance)
    except InstanceIdentityError as exc:
        raise ManifestError("invalid service instance ID") from exc
    if args.connector_id is not None:
        if args.instance == DEFAULT_INSTANCE_ID or args.component not in ("cloudflare", "openai"):
            raise ManifestError("Connector ID requires an independent ingress instance")
        try:
            valid_id(args.connector_id)
        except InstanceIdentityError as exc:
            raise ManifestError("invalid Connector ID") from exc
    if args.action != "configure" and _requested_settings(args):
        raise ManifestError("configuration options are allowed only for configure")
    if args.action == "prepare":
        if args.connector_id is not None:
            storage.prepare(args.component, args.instance, args.connector_id)
        elif args.instance != DEFAULT_INSTANCE_ID:
            storage.prepare(args.component, args.instance)
        else:
            storage.prepare(args.component)
        return {"component": args.component, "prepared": True}
    if args.action == "configure":
        return configure(args)
    manifest = (
        storage.load(args.component, args.instance, args.connector_id)
        if args.connector_id is not None
        else storage.load(args.component, args.instance)
        if args.instance != DEFAULT_INSTANCE_ID
        else storage.load(args.component)
    )
    service = Scheduler()
    if args.action == "status":
        return service.status(manifest)
    if args.action == "install":
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        return service.install(manifest)
    if args.action == "uninstall":
        return service.uninstall(manifest)
    if (
        args.action in ("start", "restart")
        and args.component == "bridge"
        and args.instance == DEFAULT_INSTANCE_ID
    ):
        if _legacy_bridge_active():
            raise TaskError("legacy same-user Bridge remains active; W3 handover is required")
    if args.action in ("start", "restart"):
        _preflight(manifest)
        _assert_instance_isolation(manifest)
        if manifest.component == "bridge" and manifest.instance_id != DEFAULT_INSTANCE_ID:
            from .legacy_bridge_ownership import assert_legacy_writable_roots_disjoint

            assert_legacy_writable_roots_disjoint(manifest)
    if args.action == "start":
        return service.start(manifest)
    if args.action == "stop":
        return service.stop(manifest)
    if args.action == "restart":
        service.stop(manifest)
        return service.start(manifest)
    raise ManifestError("unsupported service lifecycle command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except Exception as exc:  # noqa: BLE001 - fixed redacted operator messages only
        message = (
            str(exc)
            if isinstance(exc, (ManifestError, TaskError, storage.StorageError))
            else f"service refused ({type(exc).__name__})"
        )
        print("windows-v25: " + message, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
