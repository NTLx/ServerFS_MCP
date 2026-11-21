"""Portable initial Connector configuration contract."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

SPEC = importlib.util.spec_from_file_location(
    "v25_connector_provision",
    Path(__file__).resolve().parents[1] / "deployment/windows/configure-v25-connector.py",
)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def args(**kw):
    values = dict(
        instance="core-a",
        connector_id="edge-a",
        component="cloudflare",
        cloudflared=Path("cloudflared.exe"),
        cloudflared_sha256="a" * 64,
        credential_file=Path("token"),
        tunnel_client=None,
        tunnel_client_sha256=None,
        tunnel_id=None,
    )
    return SimpleNamespace(**(values | kw))


def envelope(**kw):
    doc = {"a": "a" * 32, "t": str(UUID(int=42)), "s": base64.b64encode(b"x" * 32).decode()}
    return base64.b64encode(json.dumps(doc | kw).encode())


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_single_line(suffix):
    assert tool.single_line(b"x" * 32 + suffix) == b"x" * 32


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"x" * 31,
        b"x" * 4097,
        b"x" * 32 + b"\r",
        b"x" * 32 + b"\n\n",
        b"x" * 16 + b" " + b"x" * 16,
        b"x" * 32 + b"\x00",
        b"x" * 32 + b"\xff",
    ],
)
def test_bad_credential(payload):
    with pytest.raises(tool.Refusal):
        tool.single_line(payload)


def test_cloudflare_identity():
    data = envelope()
    assert tool.cloudflare_token(data + b"\r\n") == (data, str(UUID(int=42)))


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 32,
        envelope(a="x"),
        envelope(t="bad"),
        envelope(s="invalid"),
        envelope(s=base64.b64encode(b"x").decode()),
        envelope(e=1),
        envelope(extra=True),
        base64.b64encode(b'{"a":"duplicate","a":"duplicate","s":"x","t":"x"}'),
    ],
)
def test_bad_cloudflare_envelope(payload):
    with pytest.raises(tool.Refusal):
        tool.cloudflare_token(payload)


def test_header_exact():
    assert (
        tool.header_for({"SERVERFS_HTTP_API_KEY": "x" * 64, "SERVERFS_OAUTH_ENABLED": "false"})
        == b"Bearer " + b"x" * 64
    )


@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"SERVERFS_HTTP_API_KEY": "short"},
        {"SERVERFS_HTTP_API_KEY": "x" * 64, "SERVERFS_OAUTH_ENABLED": "true"},
        {"SERVERFS_HTTP_API_KEY": "x" * 64 + "\nInjected"},
    ],
)
def test_bad_header(auth):
    with pytest.raises(tool.Refusal):
        tool.header_for(auth)


def test_options():
    tool.options(args())
    tool.options(
        args(
            component="openai",
            tunnel_client=Path("tunnel-client.exe"),
            tunnel_client_sha256="b" * 64,
            tunnel_id="tunnel_" + "c" * 32,
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"instance": "default"},
        {"connector_id": "../other"},
        {"component": "unknown"},
        {"cloudflared": Path("other.exe")},
        {"cloudflared_sha256": "A" * 64},
        {"tunnel_id": "tunnel_" + "c" * 32},
        {"component": "openai"},
        {
            "component": "openai",
            "tunnel_client": Path("other.exe"),
            "tunnel_client_sha256": "b" * 64,
            "tunnel_id": "tunnel_" + "c" * 32,
        },
        {
            "component": "openai",
            "tunnel_client": Path("tunnel-client.exe"),
            "tunnel_client_sha256": "b" * 64,
            "tunnel_id": "invalid",
        },
    ],
)
def test_bad_options(changes):
    with pytest.raises(ValueError):
        tool.options(args(**changes))


def test_port_and_host_inherited_not_operator_supplied(tmp_path):
    core = SimpleNamespace(settings={"port": 18920, "hostname": "core.local"})
    a = tool.settings_for(core, "cloudflare", tmp_path)
    b = tool.settings_for(core, "openai", tmp_path, "tunnel_" + "a" * 32)
    assert (a["port"], a["hostname"]) == (b["port"], b["hostname"]) == (18920, "core.local")
    assert a["token_file"] == str(tmp_path / "tunnel.token")
    assert b["core_auth_header_file"] == str(tmp_path / "core-auth.header")
    assert b["tunnel_id"] == "tunnel_" + "a" * 32


def test_binary_pin(tmp_path):
    import hashlib

    path = tmp_path / "cloudflared.exe"
    path.write_bytes(b"MZ" + b"x" * 2048)
    assert tool.binary_digest(path) == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("data", [b"MZ", b"not-pe" + b"x" * 2048])
def test_bad_binary(tmp_path, data):
    path = tmp_path / "cloudflared.exe"
    path.write_bytes(data)
    with pytest.raises(tool.Refusal):
        tool.binary_digest(path)


def test_peer_audit_duplicate_tunnel(tmp_path, monkeypatch):
    from serverfs_mcp.windows_v25 import storage

    peer = SimpleNamespace(component="cloudflare", instance_id="other", connector_id="edge")
    monkeypatch.setattr(storage, "home", lambda *a: tmp_path)
    monkeypatch.setattr(storage, "check_private", lambda *a, **kw: None)
    assert tool.check_generic_cf_peers([peer], "tunnel-a", "sid") == 1
    audit = tmp_path / "connector-config-attempt/audit.json"
    audit.parent.mkdir()
    audit.write_text(
        json.dumps(
            {
                "state": "COMPLETE",
                "verdict": "PASS",
                "component": "cloudflare",
                "instance": "other",
                "connector_id": "edge",
                "tunnel_identity": "tunnel-a",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(tool.Refusal, match="already allocated"):
        tool.check_generic_cf_peers([peer], "tunnel-a", "sid")
    assert tool.check_generic_cf_peers([peer], "tunnel-b", "sid") == 0
