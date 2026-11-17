"""v0.25 macOS named file-only Core: no live ServiceManagement operations."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import darwin_instance_cli, darwin_instance_runtime, darwin_instances
from serverfs_mcp.instance_identity import InstanceIdentity


def _template(tmp_path: Path) -> Path:
    app = tmp_path / "template" / darwin_instances.APP_NAME
    agents = app / "Contents/Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.ntlx.serverfs.service-manager"})
    )
    mapping = {
        "core": "http-origin",
        "bridge": "agent-bridge",
        "openai": "tunnel",
        "cloudflare": "cloudflare",
    }
    for component, service_id in mapping.items():
        label = InstanceIdentity().macos_label(component)
        (agents / f"{label}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "BundleProgram": "Contents/Resources/serverfs-service-launcher",
                    "ProgramArguments": ["serverfs-service-launcher", service_id],
                }
            )
        )
    return app


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "Library/Application Support/ServerFS"
    base.mkdir(parents=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config = tmp_path / "source.toml"
    config.write_text(f'[[workdirs]]\nalias = "data"\npath = "{workdir}"\nread_only = false\n')
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    env = private / "env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=sample-test-key\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=example.test,127.0.0.1:18781\n",
    )
    env.chmod(0o600)
    template = _template(tmp_path)
    monkeypatch.setattr(darwin_instances, "_sign_and_verify", lambda _: None)
    monkeypatch.setattr(
        darwin_instance_runtime.darwin_service, "_loopback_port_available", lambda _: True
    )
    return base, workdir, config, env, template


def _stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instance: str = "one"):
    base, workdir, config, env, template = _fixture(tmp_path, monkeypatch)
    identity = InstanceIdentity(instance_id=instance)
    result = darwin_instance_runtime.stage_file_only_instance(
        identity, config, env, 18781, "example.test", template, home=tmp_path
    )
    return identity, base, workdir, config, env, template, result


def test_stage_is_private_inactive_and_never_calls_service_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda *_args, **_kwargs: pytest.fail("stage must not register"),
    )
    identity, base, _workdir, config, env, template, result = _stage(tmp_path, monkeypatch)
    assert result["state"] == "staged-not-registered"
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    assert root == base / "instances/one/service-manager"
    assert root.stat().st_mode & 0o777 == 0o700
    manifest = root / "services.json"
    snapshot = root / "http-origin-environment.json"
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert snapshot.stat().st_mode & 0o777 == 0o600
    doc = json.loads(manifest.read_text())
    assert doc["instance_id"] == "one"
    assert doc["bridge_mode"] == "disabled"
    assert doc["bridge_id"] is None
    assert list(doc["services"]) == ["http-origin"]
    assert doc["services"]["http-origin"]["arguments"][-2:] == ["--http-port", "18781"]
    assert str(root) == str(Path(doc["services"]["http-origin"]["environment_file"]).parent)
    assert "sample-test-key" not in manifest.read_text()
    assert "sample-test-key" in snapshot.read_text()
    assert env.is_file() and config.is_file() and template.is_dir()
    assert (root / darwin_instances.APP_NAME).is_dir()


def test_two_instances_have_distinct_state_and_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity, base, *_ = _stage(tmp_path, monkeypatch)
    config2 = tmp_path / "second.toml"
    workdir2 = tmp_path / "workdir2"
    workdir2.mkdir()
    config2.write_text(f'[[workdirs]]\nalias = "data"\npath = "{workdir2}"\n')
    env2 = tmp_path / "private/env2"
    env2.write_text(
        "SERVERFS_HTTP_API_KEY=other-test-key\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=two.test,127.0.0.1:18782\n",
    )
    env2.chmod(0o600)
    second = InstanceIdentity(instance_id="two")
    result = darwin_instance_runtime.stage_file_only_instance(
        second, config2, env2, 18782, "two.test", _template(tmp_path), home=tmp_path
    )
    assert result["core_port"] == 18782
    assert second.macos_label("core") != identity.macos_label("core")
    assert darwin_instances.instance_service_home(
        second, home=tmp_path
    ) != darwin_instances.instance_service_home(identity, home=tmp_path)
    assert len(list((base / "instances").iterdir())) == 2


def test_duplicate_id_and_port_are_rejected_before_new_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity, base, _workdir, config, env, template, _ = _stage(tmp_path, monkeypatch)
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="already occupies"
    ):
        darwin_instance_runtime.stage_file_only_instance(
            identity, config, env, 18781, "example.test", template, home=tmp_path
        )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="already claimed"):
        darwin_instance_runtime.stage_file_only_instance(
            InstanceIdentity(instance_id="two"),
            config,
            env,
            18781,
            "example.test",
            template,
            home=tmp_path,
        )
    assert set(path.name for path in (base / "instances").iterdir()) == {"one"}


def test_default_staged_port_is_reserved_before_any_named_instances_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _workdir, config, env, template = _fixture(tmp_path, monkeypatch)
    old = base / "service-manager"
    old.mkdir(mode=0o700)
    legacy = old / "services.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "http-origin": {
                        "arguments": ["-m", "serverfs_mcp.cli", "serve", "--http-port", "18781"]
                    }
                },
            }
        )
    )
    legacy.chmod(0o600)
    assert not (base / "instances").exists()
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="already claimed"):
        darwin_instance_runtime.stage_file_only_instance(
            InstanceIdentity(instance_id="one"),
            config,
            env,
            18781,
            "example.test",
            template,
            home=tmp_path,
        )
    assert not (base / "instances").exists()


def test_bridge_modes_and_agent_config_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base, workdir, config, env, template = _fixture(tmp_path, monkeypatch)
    for identity in (
        InstanceIdentity(instance_id="owned", bridge_mode="owned", bridge_id="owned"),
        InstanceIdentity(instance_id="shared", bridge_mode="shared", bridge_id="default"),
    ):
        with pytest.raises(
            darwin_instance_runtime.DarwinInstanceRuntimeError, match="not yet accepted"
        ):
            darwin_instance_runtime.stage_file_only_instance(
                identity, config, env, 18781, "example.test", template, home=tmp_path
            )
    config.write_text(
        f'[agent]\nenabled = true\n[[workdirs]]\nalias = "data"\npath = "{workdir}"\n',
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="Bridge grant"):
        darwin_instance_runtime.stage_file_only_instance(
            InstanceIdentity(instance_id="one"),
            config,
            env,
            18781,
            "example.test",
            template,
            home=tmp_path,
        )
    assert not (base / "instances").exists()


def test_missing_and_wrong_auth_deny_stage_without_creating_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base, _, config, env, template = _fixture(tmp_path, monkeypatch)
    env.write_text("SERVERFS_HTTP_ALLOWED_HOSTS=example.test,127.0.0.1:18781\n")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError):
        darwin_instance_runtime.stage_file_only_instance(
            InstanceIdentity(instance_id="one"),
            config,
            env,
            18781,
            "example.test",
            template,
            home=tmp_path,
        )
    assert not (base / "instances").exists()


def test_live_config_drift_blocks_start_and_preserves_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity, _, _workdir, config, _env, _, _ = _stage(tmp_path, monkeypatch)
    config.write_text(config.read_text() + "# drift\n")
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda *_args, **_kwargs: pytest.fail("must not register"),
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="changed"):
        darwin_instance_runtime.start_component(identity, "core", home=tmp_path)


def test_start_stop_only_one_component_and_core_collision_rechecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity, _, *_ = _stage(tmp_path, monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_manager(_identity, operation, component, *, home=None):
        calls.append((operation, component))
        if operation == "status":
            return "not-registered"
        return "enabled" if operation == "register" else "not-registered"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", fake_manager)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_ready",
        lambda *_args, **_kwargs: 721,
    )
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_stopped",
        lambda *_args, **_kwargs: None,
    )
    result = darwin_instance_runtime.start_component(identity, "core", home=tmp_path)
    assert result["registration"] == "enabled"
    result = darwin_instance_runtime.stop_component(identity, "core", home=tmp_path)
    assert result["registration"] == "not-registered"
    assert calls == [
        ("status", "http-origin"),
        ("register", "http-origin"),
        ("unregister", "http-origin"),
    ]


def test_unstage_requires_inactive_and_cleans_only_owned_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    identity, base, _workdir, config, env, template, _ = _stage(tmp_path, monkeypatch)
    monkeypatch.setattr(darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "enabled")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="unregistered"):
        darwin_instance_runtime.unstage_file_only_instance(identity, home=tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    monkeypatch.setattr(darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _: None)
    darwin_instance_runtime.unstage_file_only_instance(identity, home=tmp_path)
    assert not (base / "instances/one").exists()
    assert config.exists() and env.exists() and template.exists()
    assert (base / "instances").exists()


def test_unsafe_extra_state_blocks_unstage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    identity, _, *_ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    (root / "operator-note.txt").write_text("preserve")
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    monkeypatch.setattr(darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _: None)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="unexpected"):
        darwin_instance_runtime.unstage_file_only_instance(identity, home=tmp_path)
    assert (root / "operator-note.txt").exists()


def test_named_cli_is_mac_only_and_never_routes_to_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "linux")
    assert darwin_instance_cli.main(["--instance", "test", "status"]) == 2
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    assert darwin_instance_cli.main(["--instance", "default", "status"]) == 2
    assert "legacy default" in capsys.readouterr().err


def test_named_cloudflare_stage_is_scoped_and_keeps_core_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _base, _workdir, _config, _env, _template_app, _ = _stage(tmp_path, monkeypatch)
    token = tmp_path / "private/cloudflare-token"
    token.write_text("synthetic-connector-token")
    token.chmod(0o600)
    executable = tmp_path / "private/cloudflared"
    executable.write_text("fake binary")
    executable.chmod(0o700)
    calls: list[tuple[str, str]] = []

    def manager(_identity, operation, service, *, home=None):
        calls.append((operation, service))
        if operation == "register":
            return "enabled"
        if operation == "unregister":
            return "not-registered"
        if operation == "status" and service == "http-origin":
            return "enabled"
        return "not-registered"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_ready",
        lambda *_args, **_kwargs: 722,
    )
    result = darwin_instance_runtime.stage_cloudflare_connector(
        identity, executable, token, home=tmp_path
    )
    assert result["state"] == "staged-not-registered"
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    doc = json.loads((root / "services.json").read_text())
    assert set(doc["services"]) == {"http-origin", "cloudflare"}
    assert doc["services"]["cloudflare"]["connector_owner"] == "independent-connector"
    assert doc["services"]["cloudflare"]["core_port"] == 18781
    assert (root / "services.json").stat().st_mode & 0o777 == 0o600
    assert "synthetic-connector-token" not in (root / "services.json").read_text()
    assert calls == [("status", "cloudflare")]
    result = darwin_instance_runtime.start_component(identity, "cloudflare", home=tmp_path)
    assert result["registration"] == "enabled"
    assert calls[-3:] == [
        ("status", "cloudflare"),
        ("status", "http-origin"),
        ("register", "cloudflare"),
    ]
    monkeypatch.setattr(darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _: None)
    darwin_instance_runtime.unstage_cloudflare_connector(identity, home=tmp_path)
    doc = json.loads((root / "services.json").read_text())
    assert set(doc["services"]) == {"http-origin"}
    assert token.exists()
    assert executable.exists()


def test_named_cloudflare_refuses_other_named_instance_token_file_and_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _, _, _, _, template, _ = _stage(tmp_path, monkeypatch)
    token = tmp_path / "private/shared-token"
    token.write_text("synthetic-connector-token")
    token.chmod(0o600)
    executable = tmp_path / "private/cloudflared"
    executable.write_text("fake binary")
    executable.chmod(0o700)
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    darwin_instance_runtime.stage_cloudflare_connector(first, executable, token, home=tmp_path)

    other = InstanceIdentity(instance_id="second")
    workdir2 = tmp_path / "second-workdir"
    workdir2.mkdir()
    config2 = tmp_path / "second.toml"
    config2.write_text(f'[[workdirs]]\nalias = "data"\npath = "{workdir2}"\n')
    env2 = tmp_path / "private/second.env"
    env2.write_text(
        "SERVERFS_HTTP_API_KEY=another-synthetic-key\n"
        "SERVERFS_HTTP_ALLOWED_HOSTS=second.test,127.0.0.1:18782\n"
    )
    env2.chmod(0o600)
    darwin_instance_runtime.stage_file_only_instance(
        other, config2, env2, 18782, "second.test", template, home=tmp_path
    )
    root = darwin_instances.instance_service_home(other, home=tmp_path)
    for reused in (token, tmp_path / "private/shared-hardlink"):
        if reused != token:
            os.link(token, reused)
        with pytest.raises(
            darwin_instance_runtime.DarwinInstanceRuntimeError,
            match="already belongs",
        ):
            darwin_instance_runtime.stage_cloudflare_connector(
                other, executable, reused, home=tmp_path
            )
        assert set(json.loads((root / "services.json").read_text())["services"]) == {"http-origin"}


def test_named_cloudflare_rejects_token_exposed_in_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, workdir, *_ = _stage(tmp_path, monkeypatch)
    token = workdir / "cloudflare-secret"
    token.write_text("invalid-place")
    token.chmod(0o600)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("fake")
    binary.chmod(0o700)
    monkeypatch.setattr(darwin_instance_runtime, "_manager", lambda *_a, **_kw: "not-registered")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="outside"):
        darwin_instance_runtime.stage_cloudflare_connector(identity, binary, token, home=tmp_path)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    assert set(json.loads((root / "services.json").read_text())["services"]) == {"http-origin"}


def test_file_only_data_home_and_ingress_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    env = json.loads((root / "http-origin-environment.json").read_text())
    assert env["SERVERFS_DATA_HOME"] == str(root / "data")
    assert env["SERVERFS_FILE_INGRESS_ENABLED"] == "false"
    assert (root / "data").stat().st_mode & 0o777 == 0o700
    env["SERVERFS_DATA_HOME"] = "/tmp/other-instance-data"
    snapshot = root / "http-origin-environment.json"
    snapshot.write_text(json.dumps(env))
    snapshot.chmod(0o600)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="isolation"):
        darwin_instance_runtime.start_component(identity, "core", home=tmp_path)


def test_named_cloudflare_stop_first_when_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _base, _workdir, _config, _env, _template_app, _ = _stage(tmp_path, monkeypatch)
    token = tmp_path / "private/cloudflare-token"
    token.write_text("test")
    token.chmod(0o600)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("fake")
    binary.chmod(0o700)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda *_args, **_kwargs: "not-registered",
    )
    darwin_instance_runtime.stage_cloudflare_connector(identity, binary, token, home=tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda _identity, action, component, **_kwargs: (
            "enabled" if action == "status" else "not-registered"
        ),
    )
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="Connector before"
    ):
        darwin_instance_runtime.stop_component(identity, "core", home=tmp_path)


def _predeclare_cloudflare_app(identity: InstanceIdentity, home: Path, connector_id: str) -> Path:
    app = darwin_instances.instance_app_path(identity, home=home)
    label = identity.macos_label("cloudflare", connector_id)
    plist = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "BundleProgram": "Contents/Resources/serverfs-service-launcher",
                "ProgramArguments": [
                    "serverfs-service-launcher",
                    identity.instance_id,
                    f"cloudflare.{connector_id}",
                ],
            }
        )
    )
    manager = app / "Contents/MacOS/serverfs-service-manager"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_text("synthetic manager")
    manager.chmod(0o700)
    return plist


def test_cloudflare_connector_ids_stage_independently_with_base_and_signed_plists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _, _, _, _, _ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic binary")
    binary.chmod(0o700)
    tokens = []
    for connector_id in ("blue", "green", "base"):
        token = tmp_path / f"private/token-{connector_id}"
        token.write_text(f"synthetic-{connector_id}")
        token.chmod(0o600)
        tokens.append(token)
        if connector_id != "base":
            _predeclare_cloudflare_app(identity, tmp_path, connector_id)
    monkeypatch.setattr(
        darwin_instance_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda _identity, operation, service, **_kwargs: (
            pytest.fail("staging attempted registration")
            if operation == "register"
            else "not-registered"
        ),
    )
    for connector_id, token in zip(("blue", "green", None), tokens, strict=True):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id=connector_id, home=tmp_path
        )
    doc = json.loads((root / "services.json").read_text())
    assert {"cloudflare", "cloudflare.blue", "cloudflare.green"} <= set(doc["services"])
    for key in ("cloudflare.blue", "cloudflare.green"):
        assert doc["services"][key]["stdout_log"] == str(root / f"logs/{key}.stdout.log")
        assert doc["services"][key]["stderr_log"] == str(root / f"logs/{key}.stderr.log")
    assert (root / "ServerFSServiceManager.app").is_dir()


def test_cloudflare_id_rejects_duplicate_missing_or_mismatched_signed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _, _, _, _, _ = _stage(tmp_path, monkeypatch)
    monkeypatch.setattr(
        darwin_instance_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic")
    binary.chmod(0o700)
    token = tmp_path / "private/token"
    token.write_text("synthetic")
    token.chmod(0o600)
    monkeypatch.setattr(darwin_instance_runtime, "_manager", lambda *_a, **_kw: "not-registered")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="unavailable"):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id="blue", home=tmp_path
        )
    app = darwin_instances.instance_app_path(identity, home=tmp_path)
    label = identity.macos_label("cloudflare", "blue")
    symlink_plist = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
    outside = tmp_path / "private/outside.plist"
    outside.write_bytes(plistlib.dumps({"Label": label}))
    symlink_plist.symlink_to(outside)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="unsafe"):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id="blue", home=tmp_path
        )
    symlink_plist.unlink()
    plist = _predeclare_cloudflare_app(identity, tmp_path, "blue")
    plist.write_bytes(plistlib.dumps({"Label": "wrong", "ProgramArguments": []}))
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="mismatch"):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id="blue", home=tmp_path
        )
    _predeclare_cloudflare_app(identity, tmp_path, "blue")
    darwin_instance_runtime.stage_cloudflare_connector(
        identity, binary, token, connector_id="blue", home=tmp_path
    )
    _predeclare_cloudflare_app(identity, tmp_path, "green")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="already staged"):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id="green", home=tmp_path
        )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="already staged"):
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id="blue", home=tmp_path
        )
    for invalid in ("", "Blue", "bad.id"):
        with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError):
            darwin_instance_runtime.unstage_cloudflare_connector(
                identity, connector_id=invalid, home=tmp_path
            )


def test_cloudflare_manifest_rejects_malformed_dynamic_service_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _, _, _, _, _ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    path = root / "services.json"
    document = json.loads(path.read_text())
    document["services"]["cloudflare..blue"] = {}
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="invalid Cloudflare service key"
    ):
        darwin_instance_runtime.instance_status(identity, home=tmp_path)


def test_connector_id_lifecycle_is_exactly_scoped_and_keeps_legacy_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic")
    binary.chmod(0o700)
    monkeypatch.setattr(
        darwin_instance_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    calls: list[tuple[str, str]] = []
    statuses = {"http-origin": "enabled"}

    def manager(_identity, operation, service, **_kwargs):
        calls.append((operation, service))
        if operation == "status":
            return statuses.get(service, "not-registered")
        if operation == "register":
            statuses[service] = "enabled"
            return "enabled"
        statuses.pop(service, None)
        return "not-registered"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    monkeypatch.setattr(darwin_instance_runtime, "_wait_component_ready", lambda *_a, **_kw: 741)
    monkeypatch.setattr(darwin_instance_runtime, "_wait_component_stopped", lambda *_a, **_kw: None)
    for connector_id in ("blue", "green"):
        token = tmp_path / f"private/{connector_id}-token"
        token.write_text(connector_id)
        token.chmod(0o600)
        _predeclare_cloudflare_app(identity, tmp_path, connector_id)
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id=connector_id, home=tmp_path
        )
    started = darwin_instance_runtime.start_component(
        identity, "cloudflare", connector_id="blue", home=tmp_path
    )
    stopped = darwin_instance_runtime.stop_component(
        identity, "cloudflare", connector_id="blue", home=tmp_path
    )
    assert started["component"] == stopped["component"] == "cloudflare.blue"
    assert ("register", "cloudflare.blue") in calls
    assert ("unregister", "cloudflare.blue") in calls
    assert "cloudflare.green" not in [service for op, service in calls if op == "unregister"]
    assert identity.macos_label("cloudflare", "blue").endswith(".cloudflare.blue")
    assert identity.macos_label("cloudflare", "green").endswith(".cloudflare.green")


def test_manager_passes_dynamic_register_to_native_preflight_and_surfaces_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = InstanceIdentity(instance_id="fixture")
    executable = tmp_path / "serverfs-service-manager"
    calls: list[list[str]] = []
    payload = {"ok": True, "services": {"cloudflare.blue": "enabled"}}
    monkeypatch.setattr(darwin_instance_runtime, "_manager_path", lambda *_a, **_kw: executable)

    def accepted(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(darwin_instance_runtime.subprocess, "run", accepted)
    assert darwin_instance_runtime._manager(identity, "register", "cloudflare.blue") == "enabled"
    assert calls == [[str(executable), "register", "cloudflare.blue"]]

    def refused(_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {"ok": False, "error": "dynamic Connector registration preflight failed"}
            ),
        )

    monkeypatch.setattr(darwin_instance_runtime.subprocess, "run", refused)
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="refused lifecycle"
    ):
        darwin_instance_runtime._manager(identity, "register", "cloudflare.blue")


def test_connector_id_rejects_core_invalid_ids_and_missing_signed_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    for invalid in ("", "Blue", "bad.id"):
        with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError):
            darwin_instance_runtime.start_component(
                identity, "cloudflare", connector_id=invalid, home=tmp_path
            )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="only valid"):
        darwin_instance_runtime.start_component(identity, "core", connector_id="blue")
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda *_a, **_kw: pytest.fail("invalid staged connector reached manager"),
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="not staged"):
        darwin_instance_runtime.start_component(
            identity, "cloudflare", connector_id="blue", home=tmp_path
        )


def test_connector_readiness_rollback_unregisters_only_current_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic")
    binary.chmod(0o700)
    monkeypatch.setattr(
        darwin_instance_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    for connector_id in ("blue", "green"):
        token = tmp_path / f"private/{connector_id}-token"
        token.write_text(connector_id)
        token.chmod(0o600)
        _predeclare_cloudflare_app(identity, tmp_path, connector_id)
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id=connector_id, home=tmp_path
        )
    calls: list[tuple[str, str]] = []

    def manager(_identity, operation, service, **_kwargs):
        calls.append((operation, service))
        if operation == "status":
            return "enabled" if service == "http-origin" else "not-registered"
        return "enabled" if operation == "register" else "not-registered"

    def readiness(_identity, component, _port, *, connector_id=None, **_kwargs):
        if component == "cloudflare" and connector_id == "blue":
            raise darwin_instance_runtime.DarwinInstanceRuntimeError("synthetic not ready")
        return 750

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    monkeypatch.setattr(darwin_instance_runtime, "_wait_component_ready", readiness)
    monkeypatch.setattr(darwin_instance_runtime, "_wait_component_stopped", lambda *_a, **_kw: None)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="not ready"):
        darwin_instance_runtime.start_component(
            identity, "cloudflare", connector_id="blue", home=tmp_path
        )
    assert ("unregister", "cloudflare.blue") in calls
    assert ("unregister", "cloudflare.green") not in calls


def test_cli_connector_id_parse_and_core_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = darwin_instance_cli.build_parser().parse_args(
        ["--instance", "one", "start", "--component", "cloudflare", "--connector-id", "blue"]
    )
    assert args.connector_id == "blue"
    assert args.component == "cloudflare"
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    assert (
        darwin_instance_cli.main(
            ["--instance", "one", "start", "--component", "core", "--connector-id", "blue"]
        )
        == 2
    )
    assert "cannot be used with --component core" in capsys.readouterr().err


def test_dynamic_cloudflare_unstage_is_selected_only_and_core_stop_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _, _, _, _, _ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _label: None
    )
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic")
    binary.chmod(0o700)
    for connector_id in ("blue", "green"):
        token = tmp_path / f"private/{connector_id}-token"
        token.write_text(connector_id)
        token.chmod(0o600)
        _predeclare_cloudflare_app(identity, tmp_path, connector_id)
        darwin_instance_runtime.stage_cloudflare_connector(
            identity, binary, token, connector_id=connector_id, home=tmp_path
        )
    statuses = {"cloudflare.blue": "not-registered", "cloudflare.green": "enabled"}
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda _i, op, key, **_kw: statuses.get(key, "not-registered"),
    )
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="stop the named Connector"
    ):
        darwin_instance_runtime.stop_component(identity, "core", home=tmp_path)
    statuses["cloudflare.green"] = "not-registered"
    monkeypatch.setattr(
        darwin_instance_runtime.darwin_service,
        "_launchd_job_pid",
        lambda label: 942 if label == identity.macos_label("cloudflare", "blue") else None,
    )
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="process is still running"
    ):
        darwin_instance_runtime.stop_component(identity, "core", home=tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _label: None
    )
    darwin_instance_runtime.unstage_cloudflare_connector(
        identity, connector_id="blue", home=tmp_path
    )
    services = json.loads((root / "services.json").read_text())["services"]
    assert "cloudflare.blue" not in services
    assert "cloudflare.green" in services


def test_named_readiness_failure_rolls_back_only_the_owned_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    calls: list[tuple[str, str]] = []

    def manager(_identity, action, service, *, home=None):
        calls.append((action, service))
        if action == "status":
            return "not-registered"
        return "enabled" if action == "register" else "not-registered"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)

    def not_ready(*_args, **_kwargs):
        raise darwin_instance_runtime.DarwinInstanceRuntimeError("not ready")

    monkeypatch.setattr(darwin_instance_runtime, "_wait_component_ready", not_ready)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="not ready"):
        darwin_instance_runtime.start_component(identity, "core", home=tmp_path)
    assert calls == [
        ("status", "http-origin"),
        ("register", "http-origin"),
        ("unregister", "http-origin"),
    ]


def test_named_requires_approval_rolls_back_without_starting_unauthorized_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)
    calls: list[tuple[str, str]] = []

    def manager(_identity, action, service, *, home=None):
        calls.append((action, service))
        return {
            "status": "not-registered",
            "register": "requires-approval",
            "unregister": "not-registered",
        }[action]

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_ready",
        lambda *_args, **_kwargs: pytest.fail("unapproved component must never be ready"),
    )
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_stopped",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="approval"):
        darwin_instance_runtime.start_component(identity, "core", home=tmp_path)
    assert calls == [
        ("status", "http-origin"),
        ("register", "http-origin"),
        ("unregister", "http-origin"),
    ]


def test_named_ready_failure_reports_if_rollback_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, *_ = _stage(tmp_path, monkeypatch)

    def manager(_identity, action, service, *, home=None):
        if action == "unregister":
            raise darwin_instance_runtime.DarwinInstanceRuntimeError("rollback denied")
        return "not-registered" if action == "status" else "enabled"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_wait_component_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            darwin_instance_runtime.DarwinInstanceRuntimeError("not ready")
        ),
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="rollback"):
        darwin_instance_runtime.start_component(identity, "core", home=tmp_path)


def test_named_core_listener_must_belong_to_exact_launchd_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from subprocess import CompletedProcess

    probes = []

    def lsof(argv, **kwargs):
        probes.append(tuple(argv))
        return CompletedProcess(argv, 0, "n127.0.0.1:18781\n", "")

    monkeypatch.setattr(darwin_instance_runtime.subprocess, "run", lsof)
    assert darwin_instance_runtime._core_listener_owned_by(421, 18781)
    assert probes == [
        (
            "lsof",
            "-nP",
            "-a",
            "-p",
            "421",
            "-iTCP:18781",
            "-sTCP:LISTEN",
            "-F",
            "n",
        )
    ]
    assert not darwin_instance_runtime._core_listener_owned_by(421, 18782)
    assert not darwin_instance_runtime._core_listener_owned_by(0, 18781)


def test_named_core_health_rejects_foreign_listener_even_if_http_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = InstanceIdentity(instance_id="isolated")
    monkeypatch.setattr(
        darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _label: 421
    )
    monkeypatch.setattr(darwin_instance_runtime, "_anonymous_core_401", lambda _port: True)
    monkeypatch.setattr(
        darwin_instance_runtime, "_core_listener_owned_by", lambda _pid, _port: False
    )
    monkeypatch.setattr(darwin_instance_runtime.time, "sleep", lambda _interval: None)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="not ready"):
        darwin_instance_runtime._wait_component_ready(identity, "core", 18781, timeout=0.01)
    monkeypatch.setattr(
        darwin_instance_runtime, "_core_listener_owned_by", lambda _pid, _port: True
    )
    assert (
        darwin_instance_runtime._wait_component_ready(identity, "core", 18781, timeout=0.01) == 421
    )


def test_named_cloudflare_refuses_legacy_token_path_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, base, *_ = _stage(tmp_path, monkeypatch)
    token = tmp_path / "private/cloudflare-token"
    token.write_text("synthetic-connector-token")
    token.chmod(0o600)
    binary = tmp_path / "private/cloudflared"
    binary.write_text("synthetic cloudflared")
    binary.chmod(0o700)
    default_root = base / "service-manager"
    default_root.mkdir(mode=0o700)
    legacy = default_root / "services.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "cloudflare": {"arguments": ["tunnel", "run", "--token-file", str(token)]}
                },
            }
        )
    )
    legacy.chmod(0o600)
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    with pytest.raises(
        darwin_instance_runtime.DarwinInstanceRuntimeError, match="production token"
    ):
        darwin_instance_runtime.stage_cloudflare_connector(identity, binary, token, home=tmp_path)


def test_swift_named_launcher_validates_owner_and_private_path() -> None:
    file = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceLauncher/main.swift"
    )
    source = file.read_text()
    assert 'manifest.owner == "v0.25-named-file-core"' in source
    assert "manifest.instanceID == instanceID" in source
    assert 'manifest.bridgeMode == "disabled"' in source
    assert 'manifest.services["agent-bridge"] == nil' in source
    assert "requirePrivateDirectory(instancesHome" in source
    assert "requirePrivateDirectory(instanceHome" in source
    assert "requirePrivateDirectory(serviceHome" in source


def test_swift_manager_requires_signed_owned_manifest_for_named_bridge_registration() -> None:
    file = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceManager/main.swift"
    )
    source = file.read_text()
    assert "namedOwnedRegistrationReady(instance: instance)" in source
    assert 'document["owner"] as? String == "v0.25-named-owned-core"' in source
    assert 'document["bridge_mode"] as? String == "owned"' in source
    assert "Bundle.main.bundleURL.resolvingSymlinksInPath().path == app.path" in source
    assert "named Bridge is not an authorized owned instance" in source
    assert 'args[1] == "register"' in source
    assert "Named Bridge unregister remains available" in source


def _openai_inputs(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    private = tmp_path / "private"
    binary = private / "tunnel-client"
    binary.write_bytes(b"synthetic-executable")
    binary.chmod(0o700)
    control = private / "openai-control"
    control.write_text("separate-control-secret")
    control.chmod(0o600)
    bearer = private / "core-bearer"
    bearer.write_text("Bearer sample-test-key")
    bearer.chmod(0o600)
    return binary, control, bearer, "tunnel_0123456789abcdef0123456789abcdef"


def test_named_openai_stages_direct_http_without_registering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _workdir, _config, _env, _template, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _openai_inputs(tmp_path)
    calls: list[tuple[str, str]] = []

    def manager(_identity, action, component, *, home=None):
        calls.append((action, component))
        return "not-registered"

    monkeypatch.setattr(darwin_instance_runtime, "_manager", manager)
    result = darwin_instance_runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, home=tmp_path
    )
    assert result["state"] == "staged-not-registered"
    assert calls == [("status", "tunnel")]
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    manifest = json.loads((root / "services.json").read_text())
    assert set(manifest["services"]) == {"http-origin", "tunnel"}
    spec = manifest["services"]["tunnel"]
    assert spec["connector_owner"] == "independent-openai-http"
    argv = spec["arguments"]
    assert argv[argv.index("--mcp.server-url") + 1] == "url=http://127.0.0.1:18781/mcp"
    assert argv[argv.index("--mcp.extra-headers") + 1] == f"Authorization: file:{bearer}"
    assert argv[argv.index("--mcp.discovery-extra-headers") + 1] == f"Authorization: file:{bearer}"
    assert "--mcp.command" not in argv and spec["environment_file"] is None
    assert "separate-control-secret" not in json.dumps(manifest)
    assert "sample-test-key" not in json.dumps(manifest)


def test_named_openai_rejects_wrong_and_exposed_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, workdir, _config, _env, _template, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _openai_inputs(tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    bearer.write_text("Bearer incorrect")
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="inconsistent"):
        darwin_instance_runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, bearer, home=tmp_path
        )
    bearer.write_text("Bearer sample-test-key")
    exposed = workdir / "core-bearer"
    exposed.write_bytes(bearer.read_bytes())
    exposed.chmod(0o600)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="outside"):
        darwin_instance_runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, exposed, home=tmp_path
        )
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    assert set(json.loads((root / "services.json").read_text())["services"]) == {"http-origin"}


def test_named_openai_rejects_default_tunnel_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, base, _workdir, _config, _env, _template, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _openai_inputs(tmp_path)
    legacy = base / "service-manager"
    legacy.mkdir(mode=0o700)
    manifest = legacy / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "tunnel": {
                        "arguments": ["-m", "serverfs_mcp.cli", "tunnel", "--tunnel-id", tunnel_id]
                    }
                },
            }
        )
    )
    manifest.chmod(0o600)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda *_args, **_kwargs: pytest.fail("collision must fail before registration"),
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="claimed"):
        darwin_instance_runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, bearer, home=tmp_path
        )


def test_named_openai_core_stop_refuses_active_connector_and_unstage_is_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _workdir, _config, _env, _template, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _openai_inputs(tmp_path)
    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    darwin_instance_runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, home=tmp_path
    )
    monkeypatch.setattr(
        darwin_instance_runtime,
        "_manager",
        lambda _identity, action, component, **_kwargs: (
            "enabled" if action == "status" and component == "tunnel" else "not-registered"
        ),
    )
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="Connector"):
        darwin_instance_runtime.stop_component(identity, "core", home=tmp_path)
    with pytest.raises(darwin_instance_runtime.DarwinInstanceRuntimeError, match="unregistered"):
        darwin_instance_runtime.unstage_openai_http_connector(identity, home=tmp_path)

    monkeypatch.setattr(
        darwin_instance_runtime, "_manager", lambda *_args, **_kwargs: "not-registered"
    )
    monkeypatch.setattr(darwin_instance_runtime.darwin_service, "_launchd_job_pid", lambda _: None)
    darwin_instance_runtime.unstage_openai_http_connector(identity, home=tmp_path)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    assert set(json.loads((root / "services.json").read_text())["services"]) == {"http-origin"}
    assert binary.exists() and control.exists() and bearer.exists()
