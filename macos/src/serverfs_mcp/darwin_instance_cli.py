"""Explicit macOS v0.25 named instance operator entry point.

Usage: python -m serverfs_mcp.darwin_instance_cli --instance demo stage ...
This deliberately does not change the shared `serverfs service` parser while
Windows and macOS branches are being developed concurrently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .instance_identity import InstanceIdentity, InstanceIdentityError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serverfs-macos-instance")
    parser.add_argument("--instance", required=True, help="immutable non-default instance_id")
    parser.add_argument(
        "--owned-connectors",
        action="store_true",
        help="use an already staged owned Core/Bridge for Connector operations only",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    stage = commands.add_parser("stage", help="prepare an inactive signed file-only Core")
    stage.add_argument("--config", required=True, type=Path)
    stage.add_argument("--env-file", required=True, type=Path)
    stage.add_argument("--http-port", required=True, type=int)
    stage.add_argument("--hostname", required=True)
    stage.add_argument("--template-app", required=True, type=Path)
    owned_stage = commands.add_parser(
        "owned-stage",
        help="stage a signed named Core and private owned Bridge; never register",
    )
    owned_stage.add_argument("--config", required=True, type=Path)
    owned_stage.add_argument("--env-file", required=True, type=Path)
    owned_stage.add_argument("--http-port", required=True, type=int)
    owned_stage.add_argument("--hostname", required=True)
    owned_stage.add_argument("--template-app", required=True, type=Path)
    owned_stage.add_argument("--bridge-python", required=True, type=Path)
    owned_stage.add_argument(
        "--agent-protocol",
        choices=("v1", "v2"),
        default="v1",
        help="owned Bridge protocol for this new candidate (default: legacy v1)",
    )
    for command in (stage, owned_stage):
        command.add_argument("--cloudflare-connector-id", action="append")
        command.add_argument("--openai-connector-id", action="append")
    commands.add_parser("status", help="inspect only the named file-only instance")
    commands.add_parser("owned-status", help="read-only scoped owned Core/Bridge status")
    shared_stage = commands.add_parser(
        "shared-stage", help="stage an inactive signed Shared Core with owner-supplied binding"
    )
    shared_stage.add_argument("--bridge-id", required=True)
    shared_stage.add_argument("--config", type=Path, required=True)
    shared_stage.add_argument("--env-file", type=Path, required=True)
    shared_stage.add_argument("--binding-file", type=Path, required=True)
    shared_stage.add_argument("--http-port", type=int, required=True)
    shared_stage.add_argument("--hostname", required=True)
    shared_stage.add_argument("--template-app", type=Path, required=True)
    shared_stage.add_argument("--core-python", type=Path, required=True)
    for operation, help_text in (
        (
            "shared-preflight",
            "read-only signed Core and authenticated owner v2 gate; never registers",
        ),
        (
            "shared-start",
            "register a provisioned Shared Core only after the same owner v2 gate",
        ),
    ):
        command = commands.add_parser(operation, help=help_text)
        command.add_argument("--bridge-id", required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--http-port", type=int, required=True)
    for operation in ("shared-status", "shared-stop", "shared-unstage"):
        shared_command = commands.add_parser(
            operation, help="operate only the named Shared Core, never its Bridge"
        )
        shared_command.add_argument("--bridge-id", required=True)
    owned = commands.add_parser(
        "owned-preflight",
        help="read-only owned-Bridge isolation and writable-root checks; never installs",
    )
    owned.add_argument("--config", type=Path, required=True)
    verify_owned = commands.add_parser(
        "owned-policy-check",
        help="read-only check of an already staged Bridge policy against operator TOML",
    )
    verify_owned.add_argument("--config", type=Path, required=True)
    verify_owned.add_argument("--bridge-python", type=Path, required=True)
    owned_start = commands.add_parser(
        "owned-start",
        help="explicitly register owned Bridge then Core with scoped rollback",
    )
    owned_start.add_argument("--config", type=Path, required=True)
    owned_start.add_argument("--http-port", type=int, required=True)
    commands.add_parser(
        "owned-stop",
        help="explicitly unregister owned Core then Bridge; never touch default",
    )
    commands.add_parser(
        "owned-unstage",
        help="remove only an inactive pristine named owned instance; retain all runtime evidence",
    )
    cf = commands.add_parser("cloudflare-stage", help="stage a separate Cloudflare Connector")
    cf.add_argument("--cloudflared", required=True, type=Path)
    cf.add_argument("--token-file", required=True, type=Path)
    cf.add_argument("--connector-id")
    cf_unstage = commands.add_parser(
        "cloudflare-unstage", help="remove an inactive named Connector"
    )
    cf_unstage.add_argument("--connector-id")
    openai = commands.add_parser("openai-stage", help="stage an OpenAI direct HTTP Connector")
    openai.add_argument("--tunnel-client", required=True, type=Path)
    openai.add_argument("--tunnel-id", required=True)
    openai.add_argument("--control-plane-key-file", required=True, type=Path)
    openai.add_argument("--core-bearer-file", required=True, type=Path)
    openai.add_argument("--connector-id")
    openai_unstage = commands.add_parser(
        "openai-unstage", help="remove an inactive named OpenAI Connector"
    )
    openai_unstage.add_argument("--connector-id")
    for verb in ("start", "stop"):
        cmd = commands.add_parser(verb, help=f"{verb} exactly one named component")
        cmd.add_argument("--component", choices=("core", "cloudflare", "openai"), required=True)
        cmd.add_argument("--connector-id")
    commands.add_parser("unstage", help="remove a completely inactive staged instance")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print("macOS instance lifecycle requires Darwin", file=sys.stderr)
        return 2
    from . import darwin_instance_runtime as native
    from .darwin_instances import DarwinInstanceError

    try:
        connector_operations = {
            "cloudflare-stage",
            "cloudflare-unstage",
            "openai-stage",
            "openai-unstage",
            "start",
            "stop",
        }
        if args.owned_connectors and args.operation not in connector_operations:
            raise InstanceIdentityError("--owned-connectors is only for Connector operations")
        if args.operation in {"start", "stop"}:
            if args.connector_id is not None and args.component == "core":
                raise InstanceIdentityError("--connector-id cannot be used with --component core")
            if args.owned_connectors and args.component == "core":
                raise InstanceIdentityError("--owned-connectors is only for Connector operations")
        if args.operation in {
            "shared-stage",
            "shared-preflight",
            "shared-start",
            "shared-stop",
            "shared-status",
            "shared-unstage",
        }:
            identity = InstanceIdentity(
                instance_id=args.instance,
                bridge_mode="shared",
                bridge_id=args.bridge_id,
            )
        elif (
            args.operation
            in {
                "owned-stage",
                "owned-preflight",
                "owned-policy-check",
                "owned-start",
                "owned-stop",
                "owned-unstage",
                "owned-status",
            }
            or args.owned_connectors
        ):
            identity = InstanceIdentity(
                instance_id=args.instance,
                bridge_mode="owned",
                bridge_id=args.instance,
            )
            if args.operation == "owned-preflight":
                from .darwin_instance_bridge_identity import preflight_named_owned_bridge

                result = preflight_named_owned_bridge(identity, args.config)
        else:
            identity = InstanceIdentity(instance_id=args.instance, bridge_mode="disabled")
        if args.operation == "stage":
            connector_ids = {
                provider: values
                for provider, values in (
                    ("cloudflare", args.cloudflare_connector_id),
                    ("openai", args.openai_connector_id),
                )
                if values
            }
            result = native.stage_file_only_instance(
                identity,
                args.config,
                args.env_file,
                args.http_port,
                args.hostname,
                args.template_app,
                **({"connector_ids": connector_ids} if connector_ids else {}),
            )
        elif args.operation == "status":
            result = native.instance_status(identity)
        elif args.operation == "owned-status":
            from .darwin_instance_owned_lifecycle import owned_instance_status

            result = owned_instance_status(identity)
        elif args.operation == "shared-stage":
            from .darwin_instance_shared_staging import stage_shared_core

            result = stage_shared_core(
                identity,
                args.config,
                args.env_file,
                args.binding_file,
                args.http_port,
                args.hostname,
                args.template_app,
                args.core_python,
            )
        elif args.operation == "shared-unstage":
            from .darwin_instance_shared_unstage import unstage_shared_core

            result = unstage_shared_core(identity)
        elif args.operation in {"shared-preflight", "shared-start", "shared-stop", "shared-status"}:
            from .darwin_instance_shared_lifecycle import (
                preflight_shared_core,
                shared_core_status,
                start_shared_core,
                stop_shared_core,
            )

            if args.operation == "shared-status":
                result = shared_core_status(identity)
            elif args.operation == "shared-preflight":
                result = preflight_shared_core(identity, args.config, args.http_port)
            elif args.operation == "shared-start":
                result = start_shared_core(identity, args.config, args.http_port)
            else:
                result = stop_shared_core(identity)
        elif args.operation == "owned-stage":
            from .darwin_instance_owned_runtime import stage_owned_core_bridge

            connector_ids = {
                provider: values
                for provider, values in (
                    ("cloudflare", args.cloudflare_connector_id),
                    ("openai", args.openai_connector_id),
                )
                if values
            }
            result = stage_owned_core_bridge(
                identity,
                args.config,
                args.env_file,
                args.http_port,
                args.hostname,
                args.template_app,
                args.bridge_python,
                **({"connector_ids": connector_ids} if connector_ids else {}),
                **({"agent_protocol": "v2"} if args.agent_protocol == "v2" else {}),
            )
        elif args.operation == "owned-preflight":
            pass  # read-only result was derived above; never address ServiceManagement.
        elif args.operation == "owned-policy-check":
            from .darwin_instance_owned_staging import check_owned_bridge_policy

            result = check_owned_bridge_policy(identity, args.config, args.bridge_python)
        elif args.operation == "owned-start":
            from .darwin_instance_owned_lifecycle import start_owned_pair

            result = start_owned_pair(identity, args.config, args.http_port)
        elif args.operation == "owned-stop":
            from .darwin_instance_owned_lifecycle import stop_owned_pair

            result = stop_owned_pair(identity)
        elif args.operation == "owned-unstage":
            from .darwin_instance_owned_unstage import unstage_owned_candidate

            result = unstage_owned_candidate(identity)
        elif args.operation == "cloudflare-stage":
            result = native.stage_cloudflare_connector(
                identity, args.cloudflared, args.token_file, connector_id=args.connector_id
            )
        elif args.operation == "cloudflare-unstage":
            native.unstage_cloudflare_connector(identity, connector_id=args.connector_id)
            result = {"instance_id": identity.instance_id, "state": "cloudflare-unstaged"}
        elif args.operation == "openai-stage":
            connector_kwargs = (
                {"connector_id": args.connector_id} if args.connector_id is not None else {}
            )
            result = native.stage_openai_http_connector(
                identity,
                args.tunnel_client,
                args.tunnel_id,
                args.control_plane_key_file,
                args.core_bearer_file,
                **connector_kwargs,
            )
        elif args.operation == "openai-unstage":
            connector_kwargs = (
                {"connector_id": args.connector_id} if args.connector_id is not None else {}
            )
            native.unstage_openai_http_connector(identity, **connector_kwargs)
            result = {"instance_id": identity.instance_id, "state": "openai-unstaged"}
        elif args.operation == "start":
            kwargs = {"connector_id": args.connector_id} if args.connector_id is not None else {}
            result = native.start_component(identity, args.component, **kwargs)
        elif args.operation == "stop":
            kwargs = {"connector_id": args.connector_id} if args.connector_id is not None else {}
            result = native.stop_component(identity, args.component, **kwargs)
        elif args.operation == "unstage":
            native.unstage_file_only_instance(identity)
            result = {"instance_id": identity.instance_id, "state": "unstaged"}
        else:
            raise AssertionError("unreachable parser operation")
    except (DarwinInstanceError, InstanceIdentityError, OSError, ValueError) as exc:
        print(f"ServerFS macOS instance refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
