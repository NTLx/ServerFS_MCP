"""M-C2c named OpenAI Connector staging; never register or execute a Tunnel."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import darwin_instance_cli, darwin_instances
from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.instance_identity import InstanceIdentity
from test_darwin_v0250_instance_runtime import _fixture, _openai_inputs


def _stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base, workdir, config, env, template = _fixture(tmp_path, monkeypatch)
    manager = template / "Contents/MacOS/serverfs-service-manager"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_text("synthetic manager")
    manager.chmod(0o700)
    identity = InstanceIdentity(instance_id="bluegreen")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    runtime.stage_file_only_instance(
        identity,
        config,
        env,
        18781,
        "example.test",
        template,
        connector_ids={"openai": ["blue", "green"], "cloudflare": ["blue"]},
        home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_manager", lambda *_a, **_kw: "not-registered")
    return identity, base, workdir


def _inputs(tmp_path: Path, name: str):
    binary, _control, _bearer, tunnel_id = _openai_inputs(tmp_path)
    private = tmp_path / "private"
    control = private / f"{name}-control"
    control.write_text(f"control-{name}")
    control.chmod(0o600)
    bearer = private / f"{name}-bearer"
    bearer.write_text("Bearer sample-test-key")
    bearer.chmod(0o600)
    return binary, control, bearer, tunnel_id


def test_two_named_openai_connectors_and_base_have_distinct_manifest_keys_logs_and_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    result_components = []
    for connector_id, name, suffix in (
        (None, "base", "a"),
        ("blue", "blue", "b"),
        ("green", "green", "c"),
    ):
        binary, control, bearer, tunnel_id = _inputs(tmp_path, name)
        tunnel_id = f"tunnel_{suffix * 32}"
        result = runtime.stage_openai_http_connector(
            identity,
            binary,
            tunnel_id,
            control,
            bearer,
            connector_id=connector_id,
            home=tmp_path,
        )
        result_components.append(result["component"])
    document = json.loads((root / "services.json").read_text())
    assert {"tunnel", "tunnel.blue", "tunnel.green"} <= set(document["services"])
    assert result_components == ["openai", "tunnel.blue", "tunnel.green"]
    for key, logbase in (
        ("tunnel", "openai"),
        ("tunnel.blue", "openai.blue"),
        ("tunnel.green", "openai.green"),
    ):
        spec = document["services"][key]
        assert spec["stdout_log"] == str(root / f"logs/{logbase}.stdout.log")
        assert spec["stderr_log"] == str(root / f"logs/{logbase}.stderr.log")
    token = tmp_path / "private/cloudflare-blue-token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    runtime.stage_cloudflare_connector(
        identity,
        Path(document["services"]["tunnel.blue"]["executable"]),
        token,
        connector_id="blue",
        home=tmp_path,
    )
    document = json.loads((root / "services.json").read_text())
    assert {"cloudflare.blue", "tunnel.blue"} <= set(document["services"])
    assert runtime._verify_openai_spec(root, document, 18781) is None


def test_named_openai_requires_signed_matching_plist_and_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "blue")
    app = darwin_instances.instance_app_path(identity, home=tmp_path)
    label = identity.macos_label("openai", "blue")
    plist = app / "Contents/Library/LaunchAgents" / f"{label}.plist"
    plist.unlink()
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unavailable"):
        runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
        )
    outside = tmp_path / "private/outside.plist"
    outside.write_bytes(plistlib.dumps({"Label": label}))
    plist.symlink_to(outside)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unsafe"):
        runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
        )
    plist.unlink()
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["serverfs-service-launcher", "bluegreen", "tunnel.wrong"],
            }
        )
    )
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="mismatch"):
        runtime.stage_openai_http_connector(
            identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
        )


def test_openai_tunnel_ids_credentials_and_bearer_are_independently_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, _ = _inputs(tmp_path, "blue")
    _inputs(tmp_path, "green")
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    for connector_id, name, tunnel_id in (
        ("blue", "blue", "tunnel_1" + "a" * 31),
        ("green", "green", "tunnel_2" + "b" * 31),
    ):
        _, own_control, own_bearer, _ = _inputs(tmp_path, name)
        runtime.stage_openai_http_connector(
            identity,
            binary,
            tunnel_id,
            own_control,
            own_bearer,
            connector_id=connector_id,
            home=tmp_path,
        )
    document = json.loads((root / "services.json").read_text())
    trial = json.loads(json.dumps(document))
    trial["services"]["tunnel.green"]["arguments"][-2] = "Authorization: file:/wrong"
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="arguments"):
        runtime._verify_openai_spec(root, trial, 18781)
    trial = json.loads(json.dumps(document))
    trial["services"]["tunnel.green"]["tunnel_id"] = trial["services"]["tunnel.blue"]["tunnel_id"]
    trial["services"]["tunnel.green"]["arguments"][2] = trial["services"]["tunnel.blue"][
        "tunnel_id"
    ]
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="duplicated"):
        runtime._verify_openai_spec(root, trial, 18781)
    bearer.write_text("Bearer wrong-core-key")
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="inconsistent"):
        runtime._verify_openai_spec(root, document, 18781)


def test_openai_rejects_duplicate_tunnel_id_reused_hardlink_credentials_and_invalid_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "blue")
    runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
    )
    _, control2, bearer2, _ = _inputs(tmp_path, "green")
    control2.unlink()
    control2.hardlink_to(control)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="distinct"):
        runtime.stage_openai_http_connector(
            identity,
            binary,
            "tunnel_" + "c" * 32,
            control2,
            bearer2,
            connector_id="green",
            home=tmp_path,
        )
    _, control3, bearer3, _ = _inputs(tmp_path, "other")
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="already claimed"):
        runtime.stage_openai_http_connector(
            identity,
            binary,
            tunnel_id,
            control3,
            bearer3,
            connector_id="green",
            home=tmp_path,
        )
    for invalid in ("", "Blue", "has.dot"):
        with pytest.raises(runtime.DarwinInstanceRuntimeError, match="invalid OpenAI Connector ID"):
            runtime.stage_openai_http_connector(
                identity,
                binary,
                "tunnel_" + "d" * 32,
                control3,
                bearer3,
                connector_id=invalid,
                home=tmp_path,
            )


@pytest.mark.parametrize("key_file", [False, True])
def test_named_openai_recognizes_legacy_default_tunnel_manifest(
    key_file: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, base, _ = _stage(tmp_path, monkeypatch)
    service_home = base / "service-manager"
    (service_home / "logs").mkdir(parents=True, mode=0o700)
    environment_path = service_home / "tunnel-environment.json"
    environment = {"CONTROL_PLANE_API_KEY": "synthetic-default-key"}
    environment_path.write_text(json.dumps(environment))
    environment_path.chmod(0o600)
    arguments = [
        "-m",
        "serverfs_mcp.cli",
        "tunnel",
        "--config",
        str(tmp_path / "legacy.toml"),
        "--env-file",
        str(service_home / "tunnel-proxy.env"),
        "--tunnel-id",
        "tunnel_" + "e" * 32,
    ]
    sources = [environment_path]
    if key_file:
        default_key = tmp_path / "private/default-control-key"
        default_key.write_text("synthetic-default-key")
        default_key.chmod(0o600)
        environment = {}
        environment_path.write_text(json.dumps(environment))
        arguments.extend(["--api-key-file", str(default_key)])
        sources.append(default_key)
    manifest = service_home / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "tunnel": {
                        "executable": "/private/test-python",
                        "arguments": arguments,
                        "environment_file": str(environment_path),
                        "working_directory": str(tmp_path),
                        "stdout_log": str(service_home / "logs/tunnel.stdout.log"),
                        "stderr_log": str(service_home / "logs/tunnel.stderr.log"),
                    }
                },
            }
        )
    )
    manifest.chmod(0o600)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "named")
    runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
    )
    assert all(path.exists() for path in sources)


def test_named_openai_recognizes_legacy_default_direct_http_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, base, _ = _stage(tmp_path, monkeypatch)
    service_home = base / "service-manager"
    (service_home / "logs").mkdir(parents=True, mode=0o700)
    control = tmp_path / "private/legacy-control"
    control.write_text("synthetic-legacy-control")
    control.chmod(0o600)
    bearer = tmp_path / "private/legacy-bearer"
    bearer.write_text("Bearer synthetic-legacy-core")
    bearer.chmod(0o600)
    tunnel_id = "tunnel_" + "f" * 32
    arguments = [
        "run",
        "--control-plane.tunnel-id",
        tunnel_id,
        "--control-plane.api-key",
        f"file:{control}",
        "--mcp.server-url",
        "url=http://127.0.0.1:18781/mcp",
        "--mcp.extra-headers",
        f"Authorization: file:{bearer}",
        "--mcp.discovery-extra-headers",
        f"Authorization: file:{bearer}",
        "--health.listen-addr",
        "127.0.0.1:0",
    ]
    spec = {
        "executable": "/private/test-tunnel-client",
        "arguments": arguments,
        "environment_file": None,
        "working_directory": str(tmp_path),
        "stdout_log": str(service_home / "logs/openai-connector.stdout.log"),
        "stderr_log": str(service_home / "logs/openai-connector.stderr.log"),
        "connector_owner": "independent-openai-http",
        "core_port": 18781,
    }
    manifest = service_home / "services.json"
    manifest.write_text(json.dumps({"schema_version": 1, "services": {"tunnel": spec}}))
    manifest.chmod(0o600)
    binary, named_control, named_bearer, named_id = _inputs(tmp_path, "named")
    runtime.stage_openai_http_connector(
        identity,
        binary,
        named_id,
        named_control,
        named_bearer,
        connector_id="blue",
        home=tmp_path,
    )


def test_named_openai_rejects_unknown_default_tunnel_manifest_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, base, _ = _stage(tmp_path, monkeypatch)
    service_home = base / "service-manager"
    service_home.mkdir(mode=0o700)
    manifest = service_home / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {"tunnel": {"arguments": ["--tunnel-id", "tunnel_" + "g" * 32]}},
            }
        )
    )
    manifest.chmod(0o600)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "named")
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unverifiable"):
        runtime.stage_openai_http_connector(
            identity,
            binary,
            tunnel_id,
            control,
            bearer,
            connector_id="blue",
            home=tmp_path,
        )


def test_openai_unstage_is_scoped_and_core_stop_checks_dynamic_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "blue")
    runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
    )
    monkeypatch.setattr(
        runtime,
        "_manager",
        lambda _identity, action, service, **_kwargs: (
            "enabled" if action == "status" and service == "tunnel.blue" else "not-registered"
        ),
    )
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="Connector"):
        runtime.stop_component(identity, "core", home=tmp_path)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="unregistered"):
        runtime.unstage_openai_http_connector(identity, connector_id="blue", home=tmp_path)
    monkeypatch.setattr(runtime, "_manager", lambda *_a, **_kw: "not-registered")
    monkeypatch.setattr(runtime.darwin_service, "_launchd_job_pid", lambda _label: None)
    runtime.unstage_openai_http_connector(identity, connector_id="blue", home=tmp_path)
    manifest = darwin_instances.instance_service_home(identity, home=tmp_path) / "services.json"
    doc = json.loads(manifest.read_text())
    assert "tunnel.blue" not in doc["services"]


def test_file_only_unstage_checks_named_openai_launchd_pid_before_removing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged named Tunnel must be checked under its exact native launchd identity."""
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "blue")
    runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
    )
    monkeypatch.setattr(runtime, "_manager", lambda *_args, **_kwargs: "not-registered")
    named_label = identity.macos_label("openai", "blue")
    checked: list[str] = []

    def pid_for(label: str) -> int | None:
        checked.append(label)
        return 12345 if label == named_label else None

    monkeypatch.setattr(runtime.darwin_service, "_launchd_job_pid", pid_for)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="process is still running"):
        runtime.unstage_file_only_instance(identity, home=tmp_path)
    assert named_label in checked
    assert root.is_dir()
    assert "tunnel.blue" in json.loads((root / "services.json").read_text())["services"]


def test_file_only_unstage_removes_only_inactive_named_openai_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, _, _ = _stage(tmp_path, monkeypatch)
    binary, control, bearer, tunnel_id = _inputs(tmp_path, "blue")
    runtime.stage_openai_http_connector(
        identity, binary, tunnel_id, control, bearer, connector_id="blue", home=tmp_path
    )
    monkeypatch.setattr(runtime, "_manager", lambda *_args, **_kwargs: "not-registered")
    inspected: list[str] = []

    def pid_for(label: str) -> None:
        inspected.append(label)
        return None

    monkeypatch.setattr(runtime.darwin_service, "_launchd_job_pid", pid_for)
    root = darwin_instances.instance_service_home(identity, home=tmp_path)
    runtime.unstage_file_only_instance(identity, home=tmp_path)
    assert not root.exists()
    assert identity.macos_label("openai", "blue") in inspected
    assert control.is_file() and bearer.is_file() and binary.is_file()


def test_openai_cli_forwards_optional_connector_id_and_preserves_no_id_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed = []

    def stage(_identity, *_args, **kwargs):
        observed.append(("stage", kwargs))
        return {"component": "tunnel.blue"}

    def unstage(_identity, **kwargs):
        observed.append(("unstage", kwargs))

    monkeypatch.setattr(runtime, "stage_openai_http_connector", stage)
    monkeypatch.setattr(runtime, "unstage_openai_http_connector", unstage)
    stage_args = [
        "--instance",
        "bluegreen",
        "openai-stage",
        "--tunnel-client",
        "/private/tunnel-client",
        "--tunnel-id",
        "tunnel_" + "h" * 32,
        "--control-plane-key-file",
        "/private/control-key",
        "--core-bearer-file",
        "/private/core-bearer",
        "--connector-id",
        "blue",
    ]
    assert darwin_instance_cli.main(stage_args) == 0
    assert json.loads(capsys.readouterr().out)["component"] == "tunnel.blue"
    assert (
        darwin_instance_cli.main(
            ["--instance", "bluegreen", "openai-unstage", "--connector-id", "blue"]
        )
        == 0
    )
    assert darwin_instance_cli.main(["--instance", "bluegreen", "openai-unstage"]) == 0
    assert observed == [
        ("stage", {"connector_id": "blue"}),
        ("unstage", {"connector_id": "blue"}),
        ("unstage", {}),
    ]


def test_dynamic_openai_service_manager_register_uses_native_preflight_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "serverfs-service-manager"
    monkeypatch.setattr(runtime, "_manager_path", lambda *_args, **_kwargs: executable)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "services": {"tunnel.blue": "enabled"}}),
        ),
    )
    assert (
        runtime._manager(
            InstanceIdentity(instance_id="bluegreen"), "register", "tunnel.blue", home=tmp_path
        )
        == "enabled"
    )
