"""Short-lived, no-workdir Streamable HTTP target for conformance checks."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress

import uvicorn

from serverfs_mcp.config import Settings
from serverfs_mcp.main import (
    create_server,
    streamable_http_max_request_body_size,
    streamable_http_transport_security,
)
from serverfs_mcp.workdirs import WorkdirRegistry


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp")
    return parser


def create_target(host: str, port: int, path: str):
    if not _is_loopback_host(host):
        raise ValueError("host must be a loopback address")
    authority_host = f"[{host}]" if ":" in host else host
    authority = f"{authority_host}:{port}"
    settings = Settings(
        http_host=host,
        http_port=port,
        http_path=path,
        http_allowed_hosts=(authority,),
        http_allowed_origins=(f"http://{authority}",),
    )
    registry = WorkdirRegistry([])
    return settings, registry


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings, registry = create_target(args.host, args.port, args.path)
    except ValueError as exc:
        parser.error(str(exc))
    mcp = create_server(settings, registry)
    app = mcp.streamable_http_app(
        streamable_http_path=settings.http_path,
        stateless_http=False,
        max_request_body_size=streamable_http_max_request_body_size(registry),
        transport_security=streamable_http_transport_security(settings),
        host=settings.http_host,
    )
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        access_log=False,
    )
    asyncio.run(uvicorn.Server(config).serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
