"""Windows v25: per-instance Cloudflare/OpenAI ingress contracts.

Synthetic: no Cloudflare, OpenAI Tunnel, network or Task Scheduler action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp import native_endpoint
from serverfs_mcp.windows_v25 import cli, runtime, storage
from serverfs_mcp.windows_v25.isolation import (
    InstanceCollisionError,
    assert_connector_isolation,
)
from serverfs_mcp.windows_v25.spec import Manifest, ManifestError


def core(tmp_path: Path, instance_id: str, port: int) -> Manifest:
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": instance_id,
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(tmp_path / instance_id / "envs/core/Scripts/pythonw.exe"),
            "settings": {
                "config": str(tmp_path / instance_id / "policy/serverfs.toml"),
                "auth_file": str(tmp_path / instance_id / "secret/core.env"),
                "port": port,
                "hostname": "core.example.net",
            },
        }
    )


def connector(tmp_path: Path, instance_id: str, component: str, port: int) -> Manifest:
    prefix = tmp_path / instance_id
    settings = {
        "cloudflare": {
            "cloudflared": str(prefix / "bin/cloudflared.exe"),
            "token_file": str(prefix / "secret/cf.token"),
            "port": port,
            "hostname": "core.example.net",
        },
        "openai": {
            "tunnel_client": str(prefix / "bin/tunnel-client.exe"),
            "tunnel_id": "tunnel_" + ("a" if instance_id == "staging" else "b") * 32,
            "control_plane_key_file": str(prefix / "secret/control.key"),
            "core_auth_header_file": str(prefix / "secret/core.header"),
            "port": port,
            "hostname": "core.example.net",
        },
    }
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": instance_id,
            "component": component,
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(prefix / "envs" / component / "Scripts/pythonw.exe"),
            "settings": settings[component],
        }
    )


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_v2_ingress_manifest_roundtrip_and_per_instance_task(
    tmp_path: Path, component: str
) -> None:
    a = connector(tmp_path, "staging", component, 18868)
    b = connector(tmp_path, "production", component, 18869)
    assert Manifest.parse(a.to_dict()) == a
    assert a.task_name() != b.task_name()
    assert (
        a.task_name()
        == {
            "cloudflare": "ServerFS v25 staging Cloudflare Connector",
            "openai": "ServerFS v25 staging OpenAI Connector",
        }[component]
    )
    assert a.task_argv()[-2:] == (component, "staging")
    assert b.task_argv()[-2:] == (component, "production")


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_ingress_must_target_its_own_core(tmp_path: Path, monkeypatch, component: str) -> None:
    a = core(tmp_path, "staging", 18868)
    b = connector(tmp_path, "staging", component, 18868)
    called = []

    def exact_load(name, instance_id):
        called.append((name, instance_id))
        assert name == "core" and instance_id == "staging"
        return a

    monkeypatch.setattr(storage, "load", exact_load)
    monkeypatch.setattr(runtime, "_roots", lambda config: (config, []))
    monkeypatch.setattr(runtime, "_private_input", lambda path, roots, sid: path)
    cli._preflight(b)
    assert called == [("core", "staging")]
    bad = connector(tmp_path, "staging", component, 18869)
    with pytest.raises(ManifestError, match="does not match"):
        cli._preflight(bad)


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_ingress_readiness_checks_only_own_core(
    tmp_path: Path, monkeypatch, component: str
) -> None:
    from serverfs_mcp.windows_v25 import readiness, tasks

    own = core(tmp_path, "staging", 18868)
    profile = connector(tmp_path, "staging", component, 18868)
    called: list[str] = []

    def exact_load(name, instance_id):
        assert name == "core"
        called.append(instance_id)
        return own

    monkeypatch.setattr(storage, "load", exact_load)
    monkeypatch.setattr(tasks.Scheduler, "status", lambda self, manifest: {"running": True})
    monkeypatch.setattr(readiness, "core_ready", lambda manifest: manifest is own)
    assert runtime._core_is_verified(profile)
    assert called == ["staging"]


def test_ingress_cli_configure_uses_instance_pinned_venv(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import storage as store

    sid = "S-1-5-21-111-222-333-1001"
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: sid)
    monkeypatch.setattr(
        store,
        "pinned_pythonw",
        lambda component, instance_id: (
            tmp_path / instance_id / "envs" / component / "Scripts/pythonw.exe"
        ),
    )
    expected = connector(tmp_path, "staging", "cloudflare", 18868)
    accepted = []
    monkeypatch.setattr(cli, "_preflight", lambda value: accepted.append(value))
    monkeypatch.setattr(store, "stage", lambda value: accepted.append(value))
    monkeypatch.setattr(store, "component_inventory", lambda _component: ())
    args = cli.parser().parse_args(
        [
            "cloudflare",
            "configure",
            "--instance",
            "staging",
            "--cloudflared",
            str(expected.settings["cloudflared"]),
            "--token-file",
            str(expected.settings["token_file"]),
            "--port",
            "18868",
            "--hostname",
            "core.example.net",
        ]
    )
    result = cli.dispatch(args)
    assert result["configured"] is True
    assert accepted[0].instance_id == "staging"
    assert accepted[0].task_name() == expected.task_name()
    assert accepted[0] == accepted[1]


def test_nondefault_independent_bridge_manifest_is_now_supported(tmp_path: Path) -> None:
    """v0.25 deliberately accepts separate instance-scoped Bridge owners."""
    raw = connector(tmp_path, "staging", "cloudflare", 18868).to_dict()
    raw["component"] = "bridge"
    raw["settings"] = {
        "config": str(tmp_path / "staging/policy/serverfs.toml"),
        "bridge_python": str(tmp_path / "staging/envs/bridge/Scripts/python.exe"),
    }
    parsed = Manifest.parse(raw)
    assert parsed.component == "bridge"
    assert parsed.instance_id == "staging"
    assert parsed.schema == 2
    assert parsed.task_name() == "ServerFS v25 staging Agent Bridge"
    assert Manifest.parse(parsed.to_dict()) == parsed

    broken = dict(raw)
    broken["settings"] = {**raw["settings"], "bridge_python": str(tmp_path / "bad.exe")}
    with pytest.raises(ManifestError, match="Bridge runtime interpreter"):
        Manifest.parse(broken)


def replace_settings(manifest: Manifest, **changes: str | int) -> Manifest:
    raw = manifest.to_dict()
    raw["settings"].update(changes)
    return Manifest.parse(raw)


def test_two_ingress_providers_may_share_host_for_one_core(tmp_path: Path) -> None:
    a = connector(tmp_path, "staging", "cloudflare", 18868)
    b = connector(tmp_path, "staging", "openai", 18868)
    assert_connector_isolation(a, [b])
    assert_connector_isolation(b, [a])


def test_same_core_cross_provider_private_credential_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    a = connector(tmp_path, "staging", "cloudflare", 18868)
    b = connector(tmp_path, "staging", "openai", 18868)
    b = replace_settings(b, core_auth_header_file=str(a.settings["token_file"]))
    with pytest.raises(InstanceCollisionError, match="credential file"):
        assert_connector_isolation(a, [b])
    with pytest.raises(InstanceCollisionError, match="credential file"):
        assert_connector_isolation(b, [a])


def test_exact_installed_connector_is_not_a_competing_owner(tmp_path: Path) -> None:
    a = connector(tmp_path, "staging", "cloudflare", 18868)
    assert_connector_isolation(a, [Manifest.parse(a.to_dict())])


def test_same_instance_same_provider_conflicting_manifest_is_rejected(tmp_path: Path) -> None:
    a = connector(tmp_path, "staging", "cloudflare", 18868)
    bad = replace_settings(a, token_file=str(tmp_path / "other.token"))
    with pytest.raises(InstanceCollisionError, match="instance identity"):
        assert_connector_isolation(a, [bad])


@pytest.mark.parametrize(
    ("first", "second"),
    [("cloudflare", "cloudflare"), ("cloudflare", "openai"), ("openai", "openai")],
)
def test_cross_core_internal_hostname_can_be_shared_with_independent_credentials(
    tmp_path: Path, first: str, second: str
) -> None:
    a = connector(tmp_path, "staging", first, 18868)
    b = connector(tmp_path, "production", second, 18869)
    # hostname is the local Core Host, not a public DNS allocation.
    assert a.settings["hostname"] == b.settings["hostname"]
    assert_connector_isolation(a, [b])
    assert_connector_isolation(b, [a])


def test_duplicate_cloudflare_token_path_rejected(tmp_path: Path) -> None:
    a = connector(tmp_path, "staging", "cloudflare", 18868)
    b = connector(tmp_path, "production", "cloudflare", 18869)
    bad = replace_settings(
        b, hostname="other.example.net", token_file=str(a.settings["token_file"]).upper()
    )
    with pytest.raises(InstanceCollisionError, match="credential file"):
        assert_connector_isolation(a, [bad])


@pytest.mark.parametrize(
    "resource", ["tunnel_id", "control_plane_key_file", "core_auth_header_file"]
)
def test_duplicate_openai_identity_or_private_key_refused(tmp_path: Path, resource: str) -> None:
    a = connector(tmp_path, "staging", "openai", 18868)
    b = connector(tmp_path, "production", "openai", 18869)
    bad = replace_settings(
        b,
        hostname="other.example.net",
        **{resource: a.settings[resource]},
    )
    with pytest.raises(InstanceCollisionError, match="allocated"):
        assert_connector_isolation(a, [bad])


def test_cross_provider_credential_reuse_is_rejected(tmp_path: Path) -> None:
    cf = connector(tmp_path, "staging", "cloudflare", 18868)
    openai = connector(tmp_path, "production", "openai", 18869)
    bad = replace_settings(
        openai,
        hostname="different.example.net",
        core_auth_header_file=str(cf.settings["token_file"]),
    )
    with pytest.raises(InstanceCollisionError, match="credential file"):
        assert_connector_isolation(cf, [bad])


def test_cli_does_not_publish_colliding_ingress(tmp_path: Path, monkeypatch) -> None:
    current = connector(tmp_path, "staging", "cloudflare", 18868)
    other = connector(tmp_path, "production", "openai", 18869)
    other = replace_settings(other, core_auth_header_file=str(current.settings["token_file"]))
    monkeypatch.setattr(
        storage,
        "component_inventory",
        lambda name: (other,) if name == "openai" else (),
    )
    with pytest.raises(InstanceCollisionError, match="credential file"):
        cli._assert_instance_isolation(current)


def test_runtime_refuses_colliding_connector_before_process_start(
    tmp_path: Path, monkeypatch
) -> None:
    current = connector(tmp_path, "staging", "cloudflare", 18868)
    other = connector(tmp_path, "production", "cloudflare", 18869)
    other = replace_settings(other, token_file=str(current.settings["token_file"]))
    monkeypatch.setattr(
        storage,
        "component_inventory",
        lambda name: (other,) if name == "cloudflare" else (),
    )
    monkeypatch.setattr(
        runtime,
        "_roots",
        lambda *_args: pytest.fail("must not inspect or spawn an ingress"),
    )
    with pytest.raises(InstanceCollisionError, match="credential file"):
        runtime.run_connector(current)


@pytest.mark.parametrize("component", ["cloudflare", "openai"])
def test_private_inventory_reads_only_matching_component(
    tmp_path: Path, monkeypatch, component: str
) -> None:
    import json

    from serverfs_mcp import native_lifecycle

    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "S-1-5-21-111-222-333-1001")
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: appdata, raising=False)
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)
    for instance_id in ("staging", "production"):
        value = connector(tmp_path, instance_id, component, 18868)
        raw = value.to_dict()
        raw["pythonw"] = str(storage.pinned_pythonw(component, instance_id))
        target = storage.home(component, instance_id) / "service.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(raw), encoding="utf-8")
    profiles = storage.component_inventory(component)
    assert {item.instance_id for item in profiles} == {"staging", "production"}
    opposite = "openai" if component == "cloudflare" else "cloudflare"
    assert storage.component_inventory(opposite) == ()
