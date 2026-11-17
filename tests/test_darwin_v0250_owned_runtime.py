"""macOS v0.25 owned Core+Bridge source staging and immutable wiring gates.

All tests operate on synthetic private roots. No native registration, Service-
Management execution, provider execution, or access to production credentials.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_cli, darwin_instance_owned_runtime
from serverfs_mcp.darwin_instance_owned_binding import (
    DarwinOwnedBindingError,
    resolve_owned_core_binding,
)
from serverfs_mcp.darwin_instance_runtime import darwin_service
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.darwin_owned_bridge_supervisor import (
    OwnedBridgeSupervisorError,
    verified_bridge_command,
)
from serverfs_mcp.instance_identity import InstanceIdentity

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS source-only")
BRIDGE_PYTHON = Path(__file__).resolve().parents[1] / "agent_bridge/.venv/bin/python"
PORT = 19283


def _inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    user_home = tmp_path / "user"
    root = user_home / "Library/Application Support/ServerFS"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    config = tmp_path / "serverfs.toml"
    config.write_text(
        "[agent]\nenabled = true\n"
        "[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'workspace'\n"
        f"path = '{workspace}'\n"
        "read_only = false\n"
        "agent_mode = 'workspace-write'\n"
        "agent_runtimes = ['codex']\n"
    )
    default_root = tmp_path / "default"
    default_root.mkdir(mode=0o700)
    default_config = tmp_path / "default-bridge.json"
    default_config.write_text(
        json.dumps(
            {
                "lease_key": "alias",
                "workdirs": [
                    {"alias": "default", "host_path": str(default_root), "read_only": False}
                ],
            }
        )
    )
    default_config.chmod(0o600)
    env_dir = tmp_path / "private"
    env_dir.mkdir(mode=0o700)
    env = env_dir / "env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=synthetic-unit-key\n"
        f"SERVERFS_HTTP_ALLOWED_HOSTS=synthetic.test,127.0.0.1:{PORT}\n"
    )
    env.chmod(0o600)
    monkeypatch.setattr(darwin_service, "_loopback_port_available", lambda _: True)
    monkeypatch.setattr(darwin_instance_owned_runtime, "_verify_source_app", lambda _: None)

    def fake_signed_bundle(identity, _template, destination):
        assert identity.bridge_mode == "owned"
        destination.mkdir(mode=0o700)
        return destination

    monkeypatch.setattr(darwin_instance_owned_runtime, "stage_instance_app", fake_signed_bundle)
    identity = InstanceIdentity(instance_id="owned-one", bridge_mode="owned", bridge_id="owned-one")
    return identity, user_home, config, env, default_config, workspace


def _stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not BRIDGE_PYTHON.is_file():
        pytest.skip("Bridge Python environment is unavailable")
    identity, home, config, env, default, workspace = _inputs(tmp_path, monkeypatch)
    result = darwin_instance_owned_runtime.stage_owned_core_bridge(
        identity,
        config,
        env,
        PORT,
        "synthetic.test",
        tmp_path / "fake-template",
        BRIDGE_PYTHON,
        home=home,
        default_bridge_config=default,
    )
    root = instance_service_home(identity, home=home)
    return identity, home, config, env, default, workspace, root, result


def test_owned_staging_is_private_and_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, home, config, env, default, _workspace, root, result = _stage(tmp_path, monkeypatch)
    assert result == {
        "instance_id": "owned-one",
        "bridge_mode": "owned",
        "core_port": PORT,
        "state": "staged-not-registered",
    }
    manifest = root / "services.json"
    snapshot = root / "http-origin-environment.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    doc = json.loads(manifest.read_text())
    assert doc["owner"] == "v0.25-named-owned-core"
    assert doc["bridge_id"] == identity.instance_id
    assert set(doc["services"]) == {"http-origin", "agent-bridge"}
    assert doc["services"]["http-origin"]["arguments"][-2:] == [
        "--macos-instance",
        identity.instance_id,
    ]
    assert doc["services"]["agent-bridge"]["arguments"] == [
        "-B",
        "-m",
        "serverfs_mcp.darwin_owned_bridge_supervisor",
        "--instance",
        identity.instance_id,
    ]
    assert doc["services"]["agent-bridge"]["bridge_config"] == str(
        root / "data/agent-bridge/bridge.json"
    )
    assert doc["services"]["agent-bridge"]["bridge_python"] == str(BRIDGE_PYTHON)
    assert (root / "data/agent-bridge/bridge.json").is_file()
    assert (root / APP_NAME).is_dir()
    snapshot_data = json.loads(snapshot.read_text())
    assert snapshot_data["SERVERFS_MACOS_INSTANCE_ID"] == identity.instance_id
    assert "synthetic-unit-key" not in manifest.read_text()
    assert snapshot_data["SERVERFS_DATA_HOME"] == str(root / "data")
    assert (
        resolve_owned_core_binding(
            identity,
            config,
            PORT,
            home=home,
            default_bridge_config=default,
            environ={
                "SERVERFS_MACOS_INSTANCE_ID": identity.instance_id,
                "SERVERFS_DATA_HOME": str(root / "data"),
                "SERVERFS_FILE_INGRESS_ENABLED": "false",
            },
        )[1]
        == root / "data/agent-bridge/locks"
    )


@pytest.mark.parametrize(
    "mutate, reason",
    [
        ("wrong_port", "identity"),
        ("wrong_owner", "identity"),
        ("wrong_bridge_id", "identity"),
        ("bridge_args", "launch arguments"),
        ("core_args", "launch arguments"),
        ("toml_drift", "digest"),
        ("snapshot_data_home", "environment"),
    ],
)
def test_owned_wiring_rejects_manifest_and_config_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: str, reason: str
) -> None:
    identity, home, config, _env, default, _, root, _ = _stage(tmp_path, monkeypatch)
    manifest = root / "services.json"
    doc = json.loads(manifest.read_text())
    if mutate == "wrong_port":
        doc["core"]["port"] += 1
    elif mutate == "wrong_owner":
        doc["owner"] = "v0.25-named-file-core"
    elif mutate == "wrong_bridge_id":
        doc["bridge_id"] = "other"
    elif mutate == "bridge_args":
        doc["services"]["agent-bridge"]["arguments"][-1] = "/tmp/other.json"
    elif mutate == "core_args":
        doc["services"]["http-origin"]["arguments"][-1] = "wrong"
    elif mutate == "toml_drift":
        config.write_text(config.read_text() + "\n# changed\n")
    elif mutate == "snapshot_data_home":
        snapshot_path = root / "http-origin-environment.json"
        data = json.loads(snapshot_path.read_text())
        data["SERVERFS_DATA_HOME"] = str(tmp_path / "wrong")
        snapshot_path.write_text(json.dumps(data))
        snapshot_path.chmod(0o600)
    if mutate not in {"toml_drift", "snapshot_data_home"}:
        manifest.write_text(json.dumps(doc))
        manifest.chmod(0o600)
    with pytest.raises(DarwinOwnedBindingError, match=reason):
        resolve_owned_core_binding(
            identity,
            config,
            PORT,
            home=home,
            default_bridge_config=default,
            environ={"SERVERFS_MACOS_INSTANCE_ID": identity.instance_id},
        )


@pytest.mark.parametrize(
    "env",
    [
        {"SERVERFS_MACOS_INSTANCE_ID": "other"},
        {"SERVERFS_AGENT_BRIDGE_SOCKET": "/tmp/foreign-bridge.sock"},
        {"SERVERFS_AGENT_LOCK_DIR": "/tmp/foreign-locks"},
        {"SERVERFS_DATA_HOME": "/tmp/foreign-state"},
        {"SERVERFS_FILE_INGRESS_ENABLED": "true"},
    ],
)
def test_named_core_denies_ambient_wiring_injections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    identity, home, config, _, default, _, _, _ = _stage(tmp_path, monkeypatch)
    with pytest.raises(DarwinOwnedBindingError):
        resolve_owned_core_binding(
            identity,
            config,
            PORT,
            home=home,
            default_bridge_config=default,
            environ=env,
        )


def test_owned_stage_refuses_default_writer_conflict_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, home, config, env, default, workspace = _inputs(tmp_path, monkeypatch)
    default.write_text(
        json.dumps(
            {"workdirs": [{"alias": "collision", "host_path": str(workspace), "read_only": False}]}
        )
    )
    default.chmod(0o600)
    with pytest.raises(ValueError, match="overlapping writable"):
        darwin_instance_owned_runtime.stage_owned_core_bridge(
            identity,
            config,
            env,
            PORT,
            "synthetic.test",
            tmp_path / "template",
            BRIDGE_PYTHON,
            home=home,
            default_bridge_config=default,
        )
    assert not (home / "Library/Application Support/ServerFS/instances").exists()


def test_owned_supervisor_derives_private_bridge_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, home, config, _, default, _, root, _ = _stage(tmp_path, monkeypatch)
    argv = verified_bridge_command(
        identity,
        home=home,
        default_bridge_config=default,
        environ={"SERVERFS_MACOS_INSTANCE_ID": identity.instance_id},
    )
    assert argv == (
        str(BRIDGE_PYTHON),
        "-B",
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(root / "data/agent-bridge/bridge.json"),
        "--supervised",
    )


def test_owned_supervisor_refuses_tampered_bridge_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, home, _, _, default, _, root, _ = _stage(tmp_path, monkeypatch)
    manifest = root / "services.json"
    document = json.loads(manifest.read_text())
    document["services"]["agent-bridge"]["bridge_python"] = "/tmp/wrong"
    manifest.write_text(json.dumps(document))
    manifest.chmod(0o600)
    with pytest.raises(OwnedBridgeSupervisorError, match="policy"):
        verified_bridge_command(
            identity,
            home=home,
            default_bridge_config=default,
            environ={"SERVERFS_MACOS_INSTANCE_ID": identity.instance_id},
        )


def test_named_owns_only_source_instance_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, home, config, env, default, _ = _inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="independent named owned"):
        darwin_instance_owned_runtime.stage_owned_core_bridge(
            InstanceIdentity(instance_id="owned-one", bridge_mode="disabled"),
            config,
            env,
            PORT,
            "synthetic.test",
            tmp_path / "template",
            BRIDGE_PYTHON,
            home=home,
            default_bridge_config=default,
        )


def test_named_owned_stage_cli_routes_to_owned_mode_without_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    captured = []

    def fake_stage(identity, *_args):
        captured.append((identity.instance_id, identity.bridge_mode, identity.bridge_id))
        return {"state": "staged-not-registered"}

    monkeypatch.setattr(darwin_instance_owned_runtime, "stage_owned_core_bridge", fake_stage)
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "sandbox",
                "owned-stage",
                "--config",
                str(tmp_path / "toml"),
                "--env-file",
                str(tmp_path / "env"),
                "--http-port",
                str(PORT),
                "--hostname",
                "synthetic.test",
                "--template-app",
                str(tmp_path / "app"),
                "--bridge-python",
                str(BRIDGE_PYTHON),
            ]
        )
        == 0
    )
    assert captured == [("sandbox", "owned", "sandbox")]
    assert json.loads(capsys.readouterr().out)["state"] == "staged-not-registered"


def _owned_connector_files(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    private = tmp_path / "private"
    binary = private / "synthetic-connector"
    binary.write_text("#!/bin/sh\nexec /bin/sleep 60\n")
    binary.chmod(0o700)
    token = private / "synthetic-cloudflare-token"
    token.write_text("synthetic-cloudflare-only")
    token.chmod(0o600)
    control = private / "synthetic-control"
    control.write_text("synthetic-openai-control")
    control.chmod(0o600)
    bearer = private / "synthetic-bearer"
    bearer.write_text("Bearer synthetic-unit-key")
    bearer.chmod(0o600)
    return binary, token, control, bearer, private


def test_owned_core_can_stage_both_connectors_without_rebinding_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import darwin_instance_runtime as runtime

    identity, home, config, _, default, _, root, _ = _stage(tmp_path, monkeypatch)
    binary, token, control, bearer, _ = _owned_connector_files(tmp_path)
    monkeypatch.setattr(runtime, "_manager", lambda *_args, **_kwargs: "not-registered")
    before = json.loads((root / "services.json").read_text())
    assert (
        runtime.stage_cloudflare_connector(
            identity, binary, token, home=home, default_bridge_config=default
        )["state"]
        == "staged-not-registered"
    )
    tunnel_id = "tunnel_0123456789abcdef0123456789abcdef"
    assert (
        runtime.stage_openai_http_connector(
            identity,
            binary,
            tunnel_id,
            control,
            bearer,
            home=home,
            default_bridge_config=default,
        )["component"]
        == "openai"
    )
    after = json.loads((root / "services.json").read_text())
    assert set(after["services"]) == {"agent-bridge", "http-origin", "cloudflare", "tunnel"}
    assert after["services"]["agent-bridge"] == before["services"]["agent-bridge"]
    assert after["services"]["http-origin"] == before["services"]["http-origin"]
    assert after["services"]["cloudflare"]["core_port"] == PORT
    assert after["services"]["tunnel"]["core_port"] == PORT
    assert "synthetic-unit-key" not in (root / "services.json").read_text()
    assert (
        resolve_owned_core_binding(
            identity,
            config,
            PORT,
            home=home,
            default_bridge_config=default,
            environ={"SERVERFS_MACOS_INSTANCE_ID": identity.instance_id},
        )[1]
        == root / "data/agent-bridge/locks"
    )
    runtime.unstage_openai_http_connector(identity, home=home, default_bridge_config=default)
    runtime.unstage_cloudflare_connector(identity, home=home, default_bridge_config=default)
    assert json.loads((root / "services.json").read_text()) == before
    assert control.exists() and bearer.exists() and token.exists()


@pytest.mark.parametrize(
    "tamper",
    ["wrong_cloudflare_port", "wrong_tunnel_bearer", "unknown_component", "missing_bridge"],
)
def test_owned_connector_manifest_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    from serverfs_mcp import darwin_instance_runtime as runtime

    identity, home, config, _, default, _, root, _ = _stage(tmp_path, monkeypatch)
    binary, token, control, bearer, _ = _owned_connector_files(tmp_path)
    monkeypatch.setattr(runtime, "_manager", lambda *_args, **_kwargs: "not-registered")
    runtime.stage_cloudflare_connector(
        identity, binary, token, home=home, default_bridge_config=default
    )
    runtime.stage_openai_http_connector(
        identity,
        binary,
        "tunnel_0123456789abcdef0123456789abcdef",
        control,
        bearer,
        home=home,
        default_bridge_config=default,
    )
    manifest = root / "services.json"
    doc = json.loads(manifest.read_text())
    if tamper == "wrong_cloudflare_port":
        doc["services"]["cloudflare"]["core_port"] += 1
    elif tamper == "wrong_tunnel_bearer":
        doc["services"]["tunnel"]["core_bearer_file"] = "/tmp/wrong-bearer"
    elif tamper == "unknown_component":
        doc["services"]["alien-service"] = {"executable": "/bin/sh"}
    else:
        del doc["services"]["agent-bridge"]
    manifest.write_text(json.dumps(doc))
    manifest.chmod(0o600)
    with pytest.raises(DarwinOwnedBindingError):
        resolve_owned_core_binding(
            identity,
            config,
            PORT,
            home=home,
            default_bridge_config=default,
        )


def test_owned_connector_lifecycle_is_separate_from_owned_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from serverfs_mcp import (
        darwin_instance_owned_lifecycle as lifecycle,
    )
    from serverfs_mcp import (
        darwin_instance_runtime as runtime,
    )

    identity, home, config, _, default, _, root, _ = _stage(tmp_path, monkeypatch)
    binary, token, _, _, _ = _owned_connector_files(tmp_path)
    statuses = {
        "agent-bridge": "enabled",
        "http-origin": "enabled",
        "cloudflare": "not-registered",
        "tunnel": "not-found",
    }
    events = []

    def fake_manager(_id, operation, service, *, home=None):
        assert _id == identity and home == home_path
        events.append((operation, service))
        if operation == "status":
            return statuses[service]
        if operation == "register":
            assert statuses[service] in {"not-registered", "not-found"}
            statuses[service] = "enabled"
            return "enabled"
        if operation == "unregister":
            statuses[service] = "not-registered"
            return "not-registered"
        raise AssertionError(operation)

    home_path = home
    monkeypatch.setattr(runtime, "_manager", fake_manager)
    monkeypatch.setattr(lifecycle, "_manager", fake_manager)
    runtime.stage_cloudflare_connector(
        identity, binary, token, home=home, default_bridge_config=default
    )
    monkeypatch.setattr(runtime, "_wait_component_ready", lambda *_args, **_kw: 4501)
    monkeypatch.setattr(runtime, "_wait_component_stopped", lambda *_args, **_kw: None)
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="owned Core"):
        runtime.start_component(identity, "core", home=home)
    assert (
        runtime.start_component(identity, "cloudflare", home=home, default_bridge_config=default)[
            "pid"
        ]
        == 4501
    )
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        lifecycle.stop_owned_core(identity, home=home)
    assert statuses["http-origin"] == "enabled"
    runtime.stop_component(identity, "cloudflare", home=home)
    assert statuses["cloudflare"] == "not-registered"
    assert statuses["agent-bridge"] == "enabled"
    assert ("register", "agent-bridge") not in events
    assert ("unregister", "agent-bridge") not in events


def test_owned_connector_cli_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from serverfs_mcp import darwin_instance_runtime as runtime

    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    requests = []

    def fake_start(identity, component):
        requests.append((identity.instance_id, identity.bridge_mode, component))
        return {"instance_id": identity.instance_id, "component": component}

    monkeypatch.setattr(runtime, "start_component", fake_start)
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "sandbox",
                "--owned-connectors",
                "start",
                "--component",
                "cloudflare",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["component"] == "cloudflare"
    assert requests == [("sandbox", "owned", "cloudflare")]
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "sandbox",
                "--owned-connectors",
                "owned-stop",
            ]
        )
        == 2
    )
    assert requests == [("sandbox", "owned", "cloudflare")]
