"""Fail-closed *offline* contract for two remotely managed Cloudflare routes.

No HTTP client, Cloudflare API, token handling, Windows Task Scheduler or
service lifecycle is included. The release operator supplies read-only
Cloudflare Tunnel configuration snapshots and a verified Core manifest.
This is NOT a substitute for public HTTPS/MCP acceptance.
"""

from __future__ import annotations


class TunnelRouteContractError(ValueError):
    """Fixed, non-secret route preflight refusal."""


def require(ok: bool, description: str) -> None:
    if not ok:
        raise TunnelRouteContractError(description)


def verify_single_route(
    config: object,
    *,
    public_hostname: str,
    core_hostname: str,
    port: int,
) -> None:
    """Require exactly one published route + fail-closed 404 catch-all.

    The local Host rewrite is optional only when the public hostname is
    *identical* to the Core manifest canonical Host. Distinct public DNS
    names must rewrite to the canonical Host, never widen Core's Host list.
    """
    require(
        type(public_hostname) is str
        and bool(public_hostname)
        and type(core_hostname) is str
        and bool(core_hostname)
        and type(port) is int
        and 1 <= port <= 65535,
        "route and Core identity are invalid",
    )
    require(isinstance(config, dict), "remote Tunnel configuration is malformed")
    ingress = config.get("ingress")
    require(
        isinstance(ingress, list) and len(ingress) == 2,
        "exactly one published route and one fallback required",
    )
    route, fallback = ingress
    require(isinstance(route, dict) and isinstance(fallback, dict), "Tunnel ingress malformed")
    require(
        route.get("hostname") == public_hostname
        and "path" not in route
        and route.get("service") == f"http://127.0.0.1:{port}",
        "public route or loopback origin differs from Core",
    )
    require(
        fallback == {"service": "http_status:404"},
        "Tunnel fallback must deny unknown hostnames",
    )
    options = route.get("originRequest", {})
    require(isinstance(options, dict), "route origin options are malformed")
    approved_headers = (
        (None, core_hostname) if public_hostname == core_hostname else (core_hostname,)
    )
    require(
        options.get("httpHostHeader") in approved_headers,
        "origin Host rewrite does not match Core",
    )
    require(
        options.get("noTLSVerify") is not True,
        "insecure origin TLS verification override",
    )
    # The manifest may be read from private installed storage. Never use
    # Cloudflare token contents or an opaque credentials file in this check.


def verify_dual_routes(
    routes: object,
    *,
    core_hostname: str,
    port: int,
) -> None:
    """Check two distinct independent Tunnel identities for one local Core."""
    require(
        isinstance(routes, list) and len(routes) == 2,
        "exactly two independent remote Tunnel snapshots required",
    )
    ids: set[str] = set()
    hosts: set[str] = set()
    for record in routes:
        require(isinstance(record, dict), "Tunnel identity record malformed")
        tunnel_id = record.get("tunnel_id")
        hostname = record.get("public_hostname")
        require(
            isinstance(tunnel_id, str)
            and bool(tunnel_id)
            and tunnel_id not in ids
            and isinstance(hostname, str)
            and bool(hostname)
            and hostname not in hosts,
            "Tunnel identity or public hostname is reused",
        )
        ids.add(tunnel_id)
        hosts.add(hostname)
        verify_single_route(
            record.get("config"),
            public_hostname=hostname,
            core_hostname=core_hostname,
            port=port,
        )
