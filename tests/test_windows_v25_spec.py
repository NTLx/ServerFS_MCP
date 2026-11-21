"""New Windows v25 component schema, no OS operations or service mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.spec import COMPONENTS, Manifest, ManifestError


@pytest.fixture
def source(tmp_path: Path) -> dict:
    return {
        "schema": 1,
        "component": "core",
        "sid": "S-1-5-21-111-222-333-1001",
        "pythonw": str(tmp_path / "v25/envs/core/Scripts/pythonw.exe"),
        "settings": {
            "config": str(tmp_path / "policy/serverfs.toml"),
            "auth_file": str(tmp_path / "secret/core.env"),
            "port": 18767,
            "hostname": "core.example.net",
        },
    }


def test_four_independent_tasks_never_reuse_old_task_names() -> None:
    assert COMPONENTS == {
        "core": "ServerFS v25 Core",
        "bridge": "ServerFS v25 Agent Bridge",
        "cloudflare": "ServerFS v25 Cloudflare Connector",
        "openai": "ServerFS v25 OpenAI Connector",
    }
    assert len(set(COMPONENTS.values())) == 4


def test_core_manifest_roundtrip_has_fixed_nonsecret_task_entrypoint(source: dict) -> None:
    profile = Manifest.parse(source)
    assert Manifest.parse(profile.to_dict()) == profile
    assert profile.task_name() == "ServerFS v25 Core"
    assert profile.task_argv() == (
        "-I",
        "-B",
        "-m",
        "serverfs_mcp.windows_v25.runtime",
        "core",
    )
    command = profile.task_arguments()
    assert "--auth-file" not in command
    assert "secret" not in command
    assert "windows_core_control" not in command
    assert "supervisor" not in command


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", 2),
        ("sid", "bad-sid"),
        ("pythonw", "pythonw.exe"),
        ("pythonw", "C:/exe/other.exe"),
        ("component", "legacy"),
    ],
)
def test_invalid_identity_or_schema_is_refused(source: dict, field: str, value) -> None:
    source[field] = value
    with pytest.raises(ManifestError):
        Manifest.parse(source)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("hostname", "*.example.net"),
        ("hostname", "http://localhost"),
        ("config", "relative.toml"),
    ],
)
def test_untrusted_core_setting_is_refused(source: dict, key: str, value) -> None:
    source["settings"][key] = value
    with pytest.raises(ManifestError):
        Manifest.parse(source)


def test_extra_runtime_or_secret_field_is_refused(source: dict) -> None:
    source["settings"]["command"] = "powershell.exe"
    with pytest.raises(ManifestError):
        Manifest.parse(source)
    source["settings"].pop("command")
    source["password"] = "insecure"
    with pytest.raises(ManifestError):
        Manifest.parse(source)


@pytest.mark.parametrize("component", ["bridge", "cloudflare", "openai"])
def test_other_components_allow_only_their_fixed_settings(
    source: dict, tmp_path: Path, component: str
) -> None:
    values = {
        "bridge": {
            "config": str(tmp_path / "policy/serverfs.toml"),
            "bridge_python": str(tmp_path / "envs/bridge-runtime/Scripts/python.exe"),
        },
        "cloudflare": {
            "cloudflared": str(tmp_path / "bin/cloudflared.exe"),
            "token_file": str(tmp_path / "secret/cloudflare.token"),
            "port": 18767,
            "hostname": "core.example.net",
        },
        "openai": {
            "tunnel_client": str(tmp_path / "bin/tunnel-client.exe"),
            "tunnel_id": "tunnel_" + "a" * 32,
            "control_plane_key_file": str(tmp_path / "secret/control.key"),
            "core_auth_header_file": str(tmp_path / "secret/core.header"),
            "port": 18767,
            "hostname": "core.example.net",
        },
    }
    source["component"] = component
    source["settings"] = values[component]
    assert Manifest.parse(source).component == component
    source["settings"]["extra"] = 1
    with pytest.raises(ManifestError):
        Manifest.parse(source)
