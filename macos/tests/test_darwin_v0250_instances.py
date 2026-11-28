"""v0.25 macOS per-instance ServiceManagement identity staging, never live jobs."""

from __future__ import annotations

import plistlib
import shutil
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instances
from serverfs_mcp.instance_identity import InstanceIdentity, InstanceIdentityError


def _source_app(root: Path) -> Path:
    app = root / "template" / darwin_instances.APP_NAME
    agents = app / "Contents" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    info = {
        "CFBundleIdentifier": "com.ntlx.serverfs.service-manager",
        "CFBundleExecutable": "serverfs-service-manager",
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    for component, service_id in {
        "core": "http-origin",
        "bridge": "agent-bridge",
        "openai": "tunnel",
        "cloudflare": "cloudflare",
    }.items():
        label = InstanceIdentity().macos_label(component)
        agent = {
            "Label": label,
            "BundleProgram": "Contents/Resources/serverfs-service-launcher",
            "ProgramArguments": ["serverfs-service-launcher", service_id],
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(agent))
    return app


def _stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: InstanceIdentity
) -> tuple[Path, Path, Path]:
    source = _source_app(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    target = output / darwin_instances.APP_NAME
    calls: list[Path] = []
    monkeypatch.setattr(darwin_instances, "_sign_and_verify", lambda app: calls.append(app))
    kwargs = {}
    if identity.bridge_mode == "shared":
        python = tmp_path / "synthetic-serverfs-python"
        shutil.copy2(sys.executable, python)
        python.chmod(0o700)
        kwargs["shared_core_python"] = python
    result = darwin_instances.stage_instance_app(identity, source, target, **kwargs)
    assert result == target
    assert len(calls) == 1
    assert source.exists()
    assert not list(output.glob(".serverfs-instance-*"))
    return source, target, output


def test_default_state_and_labels_remain_byte_compatible(tmp_path: Path) -> None:
    default = InstanceIdentity(instance_id="default", bridge_mode="disabled")
    assert (
        darwin_instances.instance_service_home(default, home=tmp_path)
        == tmp_path / "Library/Application Support/ServerFS/service-manager"
    )
    assert darwin_instances.instance_bundle_id(default) == ("com.ntlx.serverfs.service-manager")
    assert default.macos_label("core") == "com.ntlx.serverfs.http-origin"
    assert default.macos_label("bridge") == "com.ntlx.serverfs.bridge"
    assert default.macos_label("openai") == "com.ntlx.serverfs.tunnel"
    assert default.macos_label("cloudflare") == "com.ntlx.serverfs.cloudflare"


@pytest.mark.parametrize("raw", ["", "../bad", "A", "bad_name", "-bad", "a" * 33])
def test_invalid_instance_never_enters_bundle_staging(raw: str) -> None:
    with pytest.raises(InstanceIdentityError):
        InstanceIdentity(instance_id=raw)


def test_owned_instance_gets_unique_app_and_four_launchd_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = InstanceIdentity(instance_id="staging", bridge_mode="owned", bridge_id="staging")
    source, target, _ = _stage(tmp_path, monkeypatch, identity)
    assert (
        darwin_instances.instance_service_home(identity, home=tmp_path)
        == tmp_path / "Library/Application Support/ServerFS/instances/staging/service-manager"
    )
    info = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.ntlx.serverfs.service-manager.staging"
    assert info["ServerFSInstanceID"] == "staging"
    source_info = plistlib.loads((source / "Contents" / "Info.plist").read_bytes())
    assert "ServerFSInstanceID" not in source_info
    agents = target / "Contents" / "Library" / "LaunchAgents"
    assert len(list(agents.glob("*.plist"))) == 4
    for component, service_id in (
        ("core", "http-origin"),
        ("bridge", "agent-bridge"),
        ("openai", "tunnel"),
        ("cloudflare", "cloudflare"),
    ):
        label = identity.macos_label(component)
        entry = plistlib.loads((agents / f"{label}.plist").read_bytes())
        assert entry["Label"] == label
        assert entry["ProgramArguments"] == ["serverfs-service-launcher", "staging", service_id]
        assert entry["BundleProgram"] == "Contents/Resources/serverfs-service-launcher"
        assert entry["RunAtLoad"] and entry["KeepAlive"]


def test_predeclared_connector_ids_get_signed_bundle_service_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_app(tmp_path)
    cloudflare_base = (
        source
        / "Contents/Library/LaunchAgents"
        / (f"{InstanceIdentity().macos_label('cloudflare')}.plist")
    )
    template_spec = plistlib.loads(cloudflare_base.read_bytes())
    template_spec["ProviderTemplateField"] = {"preserved": True}
    cloudflare_base.write_bytes(plistlib.dumps(template_spec))
    output = tmp_path / "output"
    output.mkdir()
    target = output / darwin_instances.APP_NAME
    calls: list[Path] = []
    monkeypatch.setattr(darwin_instances, "_sign_and_verify", lambda app: calls.append(app))
    identity = InstanceIdentity(instance_id="staging")
    darwin_instances.stage_instance_app(
        identity, source, target, connector_ids={"cloudflare": ["blue"], "openai": ["blue"]}
    )
    assert len(calls) == 1
    agents = target / "Contents" / "Library" / "LaunchAgents"
    for component, service_key, connector_id in (
        ("cloudflare", "cloudflare.blue", "blue"),
        ("openai", "tunnel.blue", "blue"),
    ):
        label = identity.macos_label(component, connector_id)
        entry = plistlib.loads((agents / f"{label}.plist").read_bytes())
        assert label.endswith(f".{component}.{connector_id}")
        assert entry["Label"] == label
        assert entry["ProgramArguments"] == ["serverfs-service-launcher", "staging", service_key]
        if component == "cloudflare":
            assert entry["ProviderTemplateField"] == {"preserved": True}


@pytest.mark.parametrize(
    "connector_ids",
    [
        {"cloudflare": ["bad_name"]},
        {"cloudflare": ["blue", "blue"]},
        {"unknown": ["blue"]},
        {"cloudflare": "blue"},
    ],
)
def test_invalid_or_duplicate_predeclared_connector_ids_fail_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connector_ids: dict
) -> None:
    source = _source_app(tmp_path)
    target = tmp_path / "output" / darwin_instances.APP_NAME
    target.parent.mkdir()
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError):
        darwin_instances.stage_instance_app(
            InstanceIdentity(instance_id="staging"),
            source,
            target,
            connector_ids=connector_ids,
        )
    assert not target.exists()


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity(instance_id="files", bridge_mode="disabled"),
        InstanceIdentity(instance_id="client", bridge_mode="shared", bridge_id="default"),
    ],
)
def test_file_only_and_shared_client_do_not_bundle_bridge_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: InstanceIdentity
) -> None:
    _, target, _ = _stage(tmp_path, monkeypatch, identity)
    agents = target / "Contents" / "Library" / "LaunchAgents"
    assert len(list(agents.glob("*.plist"))) == 3
    assert not (agents / f"{identity.macos_label('bridge')}.plist").exists()
    assert (agents / f"{identity.macos_label('core')}.plist").exists()
    info = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
    if identity.bridge_mode == "shared":
        assert info["ServerFSSharedCorePythonPath"] == str(tmp_path / "synthetic-serverfs-python")
    else:
        assert "ServerFSSharedCorePythonPath" not in info


def test_default_install_rejected_without_touching_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_app(tmp_path)
    target = tmp_path / darwin_instances.APP_NAME
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError, match="legacy"):
        darwin_instances.stage_instance_app(InstanceIdentity(), source, target)
    assert source.exists() and not target.exists()


def test_signing_failure_removes_only_new_transient_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_app(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    target = output / darwin_instances.APP_NAME

    def refused(_: Path) -> None:
        raise darwin_instances.DarwinInstanceError("signature unavailable")

    monkeypatch.setattr(darwin_instances, "_sign_and_verify", refused)
    with pytest.raises(darwin_instances.DarwinInstanceError, match="signature unavailable"):
        darwin_instances.stage_instance_app(InstanceIdentity(instance_id="staging"), source, target)
    assert not target.exists()
    assert not list(output.iterdir())
    assert source.exists()


def test_reject_existing_destination_and_unsafe_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_app(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    target = output / darwin_instances.APP_NAME
    target.mkdir()
    monkeypatch.setattr(
        darwin_instances, "_sign_and_verify", lambda _: pytest.fail("must not sign")
    )
    with pytest.raises(darwin_instances.DarwinInstanceError, match="already exists"):
        darwin_instances.stage_instance_app(InstanceIdentity(instance_id="staging"), source, target)
    target.rmdir()

    agents = source / "Contents" / "Library" / "LaunchAgents"
    poison = agents / "com.ntlx.serverfs.bridge.plist"
    entry = plistlib.loads(poison.read_bytes())
    entry["Label"] = "com.ntlx.serverfs.other.bridge"
    poison.write_bytes(plistlib.dumps(entry))
    with pytest.raises(darwin_instances.DarwinInstanceError, match="identity mismatch"):
        darwin_instances.stage_instance_app(InstanceIdentity(instance_id="staging"), source, target)
    assert not target.exists()


def test_native_launcher_matches_bundle_id_and_named_manifest_path() -> None:
    root = Path(__file__).resolve().parents[1] / "native/macos/service_manager/Sources"
    manager = (root / "ServiceManager/main.swift").read_text(encoding="utf-8")
    launcher = (root / "ServiceLauncher/main.swift").read_text(encoding="utf-8")
    assert "ServerFSInstanceID" in manager and "ServerFSInstanceID" in launcher
    assert "non-default instances require explicit component" in manager
    assert "com.ntlx.serverfs.\\(instance).\\(component).plist" in manager
    assert "args[1] == instance" in launcher
    assert "instances/\\(instanceID)/service-manager/services.json" in launcher
    assert 'instanceID == "default"' in launcher
    assert "connectorServicePattern" in launcher
    assert "unknownServiceKeys.isEmpty" in launcher
    assert "validConnectorID" in manager
    assert 'args[1] == "register", selected.contains(where: { $0.id.contains(".") })' in manager
    assert "manifest.services[serviceID] != nil" in launcher
    assert 'info["CFBundleIdentifier"] as? String == expectedBundleID' in launcher
    assert (
        'ProgramArguments"] as? [String] == ["serverfs-service-launcher", instance, raw]'
    ) in manager
