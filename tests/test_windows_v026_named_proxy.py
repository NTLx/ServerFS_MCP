"""Portable contracts for Windows named runtime proxy admission (v0.26.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.named_agent_proxy import (
    NamedAgentProxyError,
    load_named_agent_proxy,
    parse_proxy_source,
    uses_agent_proxy,
)


def test_explicit_provider_and_acp_opt_in() -> None:
    assert uses_agent_proxy({"runtimes": {"claude": {"enabled": True, "use_proxy": True}}})
    assert uses_agent_proxy({"acp_profiles": [{"use_proxy": True}]})
    assert not uses_agent_proxy({"runtimes": {"claude": {"enabled": True, "use_proxy": False}}})


@pytest.mark.parametrize(
    "policy",
    [
        {"runtimes": {"claude": {"enabled": False, "use_proxy": True}}},
        {"runtimes": {"claude": {"enabled": True, "use_proxy": "true"}}},
        {"runtimes": {"unknown": {"enabled": True, "use_proxy": True}}},
        {"acp_profiles": [{"use_proxy": "true"}]},
    ],
)
def test_invalid_opt_in_refused(policy: dict) -> None:
    with pytest.raises(NamedAgentProxyError):
        uses_agent_proxy(policy)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"SERVERFS_AGENT_PROXY_URL=",
        b"BROKEN=1",
        b"SERVERFS_AGENT_PROXY_URL=http://u:p@host:3128",
        b"SERVERFS_AGENT_PROXY_URL=socks5://host:1080",
        b"SERVERFS_AGENT_PROXY_URL=http://host:3128\nSERVERFS_AGENT_PROXY_URL=http://other:3128",
        b"SERVERFS_AGENT_PROXY_URL=http://host:3128\rBROKEN=value",
    ],
)
def test_private_source_refused(raw: bytes) -> None:
    with pytest.raises(NamedAgentProxyError):
        parse_proxy_source(raw)


def test_private_source_accepts_credentialless_http_and_forces_loopback_bypass() -> None:
    proxy = parse_proxy_source(
        b"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170\r\n"
        b"SERVERFS_AGENT_NO_PROXY=example.local\r\n"
    )
    assert proxy.url == "http://127.0.0.1:20170"
    assert {"127.0.0.1", "localhost", "::1"}.issubset(proxy.no_proxy.split(","))
    assert "example.local" in proxy.no_proxy.split(",")



def test_named_owner_requires_valid_private_file_when_runtime_opts_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp.windows_v25 import storage

    monkeypatch.setattr(storage, "check_private", lambda *a, **kw: None)
    enabled = {"claude": {"enabled": True, "use_proxy": True}}
    with pytest.raises(NamedAgentProxyError):
        load_named_agent_proxy(bridge_home=tmp_path, sid="synthetic", bridge_document=enabled)
    file = tmp_path / "agent-proxy.env"
    file.write_bytes(b"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170\n")
    proxy = load_named_agent_proxy(bridge_home=tmp_path, sid="synthetic", bridge_document=enabled)
    assert proxy is not None and proxy.url == "http://127.0.0.1:20170"
    file.write_bytes(b"SERVERFS_AGENT_PROXY_URL=http://user:secret@proxy:20170\n")
    with pytest.raises(NamedAgentProxyError):
        load_named_agent_proxy(bridge_home=tmp_path, sid="synthetic", bridge_document=enabled)


def test_private_bootstrap_contains_endpoint_but_bridge_child_environment_does_not() -> None:
    import json

    from serverfs_mcp.agent_lifecycle import bootstrap_frame_bytes, bridge_child_environment

    proxy = parse_proxy_source(b"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170\n")
    frame = json.loads(bootstrap_frame_bytes(proxy, prior_bridge_execution_stopped=True))
    assert frame["agent_proxy"]["enabled"] is True
    assert frame["agent_proxy"]["url"] == proxy.url
    assert frame["prior_bridge_execution_stopped"] is True
    child = bridge_child_environment(
        proxy,
        source={
            "PATH": "test-native-executable-path",
            "HTTPS_PROXY": "http://untrusted.example:3333",
            "SERVERFS_AGENT_PROXY_URL": proxy.url,
            "SERVERFS_PROXY_TOKEN": "synthetic",
        },
    )
    assert child == {"PATH": "test-native-executable-path"}


def test_named_owner_reopens_its_private_source_not_process_proxy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    from serverfs_mcp.windows_v25 import storage
    from serverfs_mcp.windows_v25.named_agent_proxy import read_owned_named_agent_proxy

    monkeypatch.setattr(storage, "home", lambda component, instance_id: tmp_path)
    monkeypatch.setattr(storage, "check_private", lambda *a, **kw: None)
    manifest = SimpleNamespace(instance_id="named-owner", sid="synthetic")
    config = tmp_path / "bridge.json"
    config.write_text(
        json.dumps({"bridge_id": "named-owner", "claude": {"enabled": True, "use_proxy": True}}),
        encoding="utf-8",
    )
    (tmp_path / "agent-proxy.env").write_bytes(
        b"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170\n"
    )
    monkeypatch.setenv("SERVERFS_AGENT_PROXY_URL", "http://ambient.invalid:3333")
    proxy = read_owned_named_agent_proxy(manifest=manifest, config_path=config)
    assert proxy is not None and proxy.url == "http://127.0.0.1:20170"

def test_unproxied_owner_does_not_touch_proxy_file(tmp_path: Path) -> None:
    assert (
        load_named_agent_proxy(
            bridge_home=tmp_path,
            sid="synthetic",
            bridge_document={"claude": {"enabled": True, "use_proxy": False}},
        )
        is None
    )
