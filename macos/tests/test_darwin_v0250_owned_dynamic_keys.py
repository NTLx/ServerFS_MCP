"""macOS v0.25 owned Core manifest accepts only exact per-instance service keys."""

import pytest

from serverfs_mcp.darwin_instance_owned_binding import _owned_service_supported


@pytest.mark.parametrize(
    "key",
    [
        "http-origin",
        "agent-bridge",
        "cloudflare",
        "tunnel",
        "cloudflare.blue",
        "cloudflare.blue-2",
        "tunnel.blue",
        "tunnel.green",
    ],
)
def test_owned_core_accepts_supported_connector_keys(key: str) -> None:
    assert _owned_service_supported(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "bridge",
        "core",
        "openai.blue",
        "cloudflare.",
        "tunnel.",
        "cloudflare.Blue",
        "tunnel..blue",
        "tunnel.blue.green",
        "cloudflare.-bad",
        "tunnel.bad_underscore",
    ],
)
def test_owned_core_rejects_unknown_or_malformed_connector_keys(key: str) -> None:
    assert not _owned_service_supported(key)
