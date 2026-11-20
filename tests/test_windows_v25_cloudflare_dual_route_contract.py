"""Source-only routes contract for two independent Cloudflare Tunnels per Core."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-cloudflare-dual-route-contract.py"
)
_SPEC = importlib.util.spec_from_file_location("v0250_dual_route_contract", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
contract = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(contract)

_CORE = "private-core.example.test"
_PORT = 18871


def _record(tunnel: str, public: str) -> dict:
    return {
        "tunnel_id": tunnel,
        "public_hostname": public,
        "config": {
            "ingress": [
                {
                    "hostname": public,
                    "service": f"http://127.0.0.1:{_PORT}",
                    "originRequest": {"httpHostHeader": _CORE},
                },
                {"service": "http_status:404"},
            ]
        },
    }


def test_two_independent_remote_tunnels_share_one_canonical_private_core() -> None:
    entries = [
        _record("tunnel-a", "public-a.example.test"),
        _record("tunnel-b", "public-b.example.test"),
    ]
    contract.verify_dual_routes(entries, core_hostname=_CORE, port=_PORT)


@pytest.mark.parametrize(
    ("bad_target", "bad_value"),
    [
        ("service", "http://127.0.0.1:18767"),
        ("service", "http://0.0.0.0:18871"),
        ("service", "https://127.0.0.1:18871"),
        ("hostname", "wrong.example.test"),
        ("path", "/other"),
    ],
)
def test_route_must_match_exact_private_origin_and_public_name(
    bad_target: str, bad_value: str
) -> None:
    record = _record("tunnel-a", "public-a.example.test")
    record["config"]["ingress"][0][bad_target] = bad_value
    with pytest.raises(contract.TunnelRouteContractError):
        contract.verify_single_route(
            record["config"],
            public_hostname=record["public_hostname"],
            core_hostname=_CORE,
            port=_PORT,
        )


def test_distinct_public_host_requires_origin_host_rewrite() -> None:
    record = _record("tunnel-a", "public-a.example.test")
    options = record["config"]["ingress"][0]["originRequest"]
    del options["httpHostHeader"]
    with pytest.raises(contract.TunnelRouteContractError, match="Host rewrite"):
        contract.verify_dual_routes(
            [record, _record("tunnel-b", "public-b.example.test")], core_hostname=_CORE, port=_PORT
        )
    options["httpHostHeader"] = "public-a.example.test"
    with pytest.raises(contract.TunnelRouteContractError, match="Host rewrite"):
        contract.verify_single_route(
            record["config"],
            public_hostname=record["public_hostname"],
            core_hostname=_CORE,
            port=_PORT,
        )


def test_same_public_host_as_core_may_use_existing_remote_route_without_override() -> None:
    # Current production workpc-serverfs route has no origin Host override.
    # No change to that production Tunnel is required for its existing host.
    record = _record("existing", "workpc-serverfs.ntlx.top")
    record["config"]["ingress"][0]["service"] = "http://127.0.0.1:18767"
    del record["config"]["ingress"][0]["originRequest"]
    contract.verify_single_route(
        record["config"],
        public_hostname="workpc-serverfs.ntlx.top",
        core_hostname="workpc-serverfs.ntlx.top",
        port=18767,
    )
    with pytest.raises(contract.TunnelRouteContractError, match="Host rewrite"):
        contract.verify_single_route(
            record["config"],
            public_hostname="workpc-serverfs.ntlx.top",
            core_hostname="different-core.ntlx.top",
            port=18767,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        {"service": "http_status:200"},
        {"service": "http_status:503"},
        {"hostname": "other.example.test", "service": "http_status:404"},
        {"service": "http_status:404", "originRequest": {}},
    ],
)
def test_fallback_must_be_only_deny_all_404(invalid: dict) -> None:
    record = _record("tunnel-a", "public-a.example.test")
    record["config"]["ingress"][-1] = invalid
    with pytest.raises(contract.TunnelRouteContractError, match="fallback"):
        contract.verify_single_route(
            record["config"],
            public_hostname=record["public_hostname"],
            core_hostname=_CORE,
            port=_PORT,
        )


def test_no_extra_host_ingress_or_origin_tls_bypass() -> None:
    record = _record("tunnel-a", "public-a.example.test")
    copied = copy.deepcopy(record)
    copied["config"]["ingress"].insert(
        1, _record("extra", "unexpected.example.test")["config"]["ingress"][0]
    )
    with pytest.raises(contract.TunnelRouteContractError, match="exactly one"):
        contract.verify_single_route(
            copied["config"],
            public_hostname=copied["public_hostname"],
            core_hostname=_CORE,
            port=_PORT,
        )
    record["config"]["ingress"][0]["originRequest"]["noTLSVerify"] = True
    with pytest.raises(contract.TunnelRouteContractError, match="TLS"):
        contract.verify_single_route(
            record["config"],
            public_hostname=record["public_hostname"],
            core_hostname=_CORE,
            port=_PORT,
        )


@pytest.mark.parametrize("which", ["id", "public"])
def test_same_tunnel_or_duplicate_public_hostname_fails_closed(which: str) -> None:
    first = _record("tunnel-a", "public-a.example.test")
    second = _record(
        "tunnel-a" if which == "id" else "tunnel-b",
        "public-b.example.test" if which == "id" else "public-a.example.test",
    )
    with pytest.raises(contract.TunnelRouteContractError, match="reused"):
        contract.verify_dual_routes([first, second], core_hostname=_CORE, port=_PORT)


def test_route_check_does_not_launch_cloudflare_or_access_tokens() -> None:
    content = _SOURCE.read_text("utf-8")
    for forbidden in (
        "requests.get(",
        "cloudflare.request(",
        "subprocess.Popen(",
        "Scheduler().start(",
        "token_file",
        "tunnel --token",
        "git push",
    ):
        assert forbidden not in content
