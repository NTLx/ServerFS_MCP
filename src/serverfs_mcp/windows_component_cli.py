"""Current-user Windows v0.25 independent component lifecycle operator entrypoint.

Invoke from the appropriate dedicated Product/Native Python environment:
    python -I -m serverfs_mcp.windows_component_cli core status
    python -I -m serverfs_mcp.windows_component_cli bridge status
    python -I -m serverfs_mcp.windows_component_cli cloudflare status

Only the explicit new component Task Scheduler contracts are reachable here.
Legacy 'serverfs service' commands retain their own rollback behavior.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_COMPONENTS = ("core", "bridge", "cloudflare", "openai")
_ACTIONS = ("status", "install", "start", "stop", "restart", "pause", "uninstall")


class ComponentCLIError(Exception):
    """Redacted operator error for missing required component inputs."""


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="serverfs-windows-component")
    cli.add_argument("component", choices=_COMPONENTS)
    cli.add_argument("action", choices=_ACTIONS)
    cli.add_argument("--config", type=Path)
    cli.add_argument("--auth-file", type=Path)
    cli.add_argument("--port", type=int)
    cli.add_argument("--hostname")
    return cli


def _require_install_options(args: argparse.Namespace) -> None:
    if args.action == "install":
        if args.component == "bridge":
            if args.config is None:
                raise ComponentCLIError("Bridge installation requires --config")
        elif args.component == "core":
            if any(
                item is None for item in (args.config, args.auth_file, args.port, args.hostname)
            ):
                raise ComponentCLIError(
                    "Core installation requires --config, --auth-file, --port and --hostname"
                )
    elif any(item is not None for item in (args.config, args.auth_file, args.port, args.hostname)):
        raise ComponentCLIError("configuration arguments are only accepted for installation")
    if (
        args.action == "install"
        and args.component in ("cloudflare", "openai")
        and any(
            item is not None for item in (args.config, args.auth_file, args.port, args.hostname)
        )
    ):
        raise ComponentCLIError("connector policy must already exist in its private service home")


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """One exact component action, no shell, legacy fallback or unsafe argv."""
    _require_install_options(args)
    component, action = args.component, args.action
    if component == "bridge":
        from . import windows_bridge_control as control

        if action == "install":
            return control.install_disabled(args.config)
        if action == "status":
            return control.status()
        if action == "start":
            return control.start()
        if action == "stop":
            return control.stop()
        if action == "restart":
            return control.restart()
        if action == "pause":
            return control.pause()
        control.uninstall_offline()
    elif component == "core":
        from . import windows_core_control as control

        if action == "install":
            from .native_endpoint import current_user_sid
            from .windows_core_task_spec import CoreTaskSpec

            spec = CoreTaskSpec(
                sid=current_user_sid(),
                pythonw=Path(sys.prefix) / "Scripts" / "pythonw.exe",
                config_path=args.config,
                auth_file=args.auth_file,
                port=args.port,
                hostname=args.hostname,
            )
            return control.install_disabled(spec)
        if action == "status":
            return control.status()
        if action == "start":
            return control.start()
        if action == "stop":
            return control.stop()
        if action == "restart":
            return control.restart()
        if action == "pause":
            return control.pause()
        control.uninstall_offline()
    else:
        from . import windows_connector_control as control

        if action == "install":
            return control.install_disabled(component)
        if action == "status":
            return control.status(component)
        if action == "start":
            return control.start(component)
        if action == "stop":
            return control.stop(component)
        if action == "restart":
            return control.restart(component)
        if action == "pause":
            return control.pause(component)
        control.uninstall_offline(component)
    return {"component": component, "uninstalled": True}


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("serverfs-windows-component: Windows is required", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        snapshot = dispatch(args)
    except Exception as exc:  # noqa: BLE001 - no paths/credentials in operator diagnostics
        from .windows_bridge_control import BridgeControlError
        from .windows_connector_control import ConnectorControlError
        from .windows_core_control import CoreControlError
        from .windows_core_task_spec import CoreTaskSpecError

        if isinstance(
            exc,
            (
                BridgeControlError,
                ConnectorControlError,
                CoreControlError,
                CoreTaskSpecError,
                ComponentCLIError,
            ),
        ):
            detail = str(exc)
        else:
            detail = f"component operation failed ({type(exc).__name__})"
        print(f"serverfs-windows-component: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
