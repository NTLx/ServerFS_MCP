"""macOS named owned lifecycle ordering, rollback and IPC readiness tests.

All native ServiceManagement calls are replaced with fake per-instance state.
No process registration, real provider, system service or Git action.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import darwin_instance_cli
from serverfs_mcp import darwin_instance_owned_lifecycle as lifecycle
from serverfs_mcp import darwin_instance_runtime as runtime
from serverfs_mcp.instance_identity import InstanceIdentity


def _id() -> InstanceIdentity:
    return InstanceIdentity(instance_id="owned-test", bridge_mode="owned", bridge_id="owned-test")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    root.mkdir(parents=True, mode=0o700)
    (root / "services.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": "owned-test",
                "bridge_mode": "owned",
                "bridge_id": "owned-test",
                "core": {"port": 19283},
                "services": {"agent-bridge": {}, "http-origin": {}, "cloudflare": {}, "tunnel": {}},
            }
        )
    )
    (root / "services.json").chmod(0o600)
    socket_path = tmp_path / "owned.sock"
    calls: list[tuple[str, str]] = []
    statuses = {"agent-bridge": "not-registered", "http-origin": "not-registered"}

    def manager(identity, operation, component, *, home=None):
        assert identity == _id()
        assert home == tmp_path
        calls.append((operation, component))
        if component not in statuses:
            assert operation == "status"
            return "not-found"
        if operation == "status":
            return statuses[component]
        if operation == "register":
            assert statuses[component] == "not-registered"
            statuses[component] = "enabled"
            return "enabled"
        if operation == "unregister":
            statuses[component] = "not-registered"
            return "not-registered"
        raise AssertionError(operation)

    monkeypatch.setattr(lifecycle, "_manager", manager)
    monkeypatch.setattr(
        lifecycle,
        "resolve_owned_core_binding",
        lambda *_args, **_kwargs: (str(socket_path), root / "data/agent-bridge/locks"),
    )
    monkeypatch.setattr(
        lifecycle,
        "named_owned_bridge_resources",
        lambda *_args, **_kwargs: type("Resources", (), {"socket_path": socket_path})(),
    )
    monkeypatch.setattr(lifecycle, "_assert_available_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_bridge_ready", lambda *_args, **_kwargs: 4111)
    monkeypatch.setattr(lifecycle, "_core_ready", lambda *_args, **_kwargs: 4222)
    monkeypatch.setattr(lifecycle, "_wait_component_stopped", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle.darwin_service, "_launchd_job_pid", lambda _label: None)
    return statuses, calls, socket_path


def test_owned_pair_registers_bridge_then_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)
    result = lifecycle.start_owned_pair(
        _id(),
        tmp_path / "operator.toml",
        19283,
        home=tmp_path,
    )
    assert result["state"] == "enabled"
    assert result["bridge_pid"] == 4111 and result["core_pid"] == 4222
    assert statuses == {"agent-bridge": "enabled", "http-origin": "enabled"}
    assert [component for operation, component in calls if operation == "register"] == [
        "agent-bridge",
        "http-origin",
    ]


def test_owned_pair_core_readiness_failure_rolls_back_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)

    def failure(*_args, **_kwargs):
        raise lifecycle.DarwinOwnedLifecycleError("Core has no listener")

    monkeypatch.setattr(lifecycle, "_core_ready", failure)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Bridge rolled back"):
        lifecycle.start_owned_pair(_id(), tmp_path / "operator.toml", 19283, home=tmp_path)
    assert statuses == {"agent-bridge": "not-registered", "http-origin": "not-registered"}
    assert [component for operation, component in calls if operation == "unregister"] == [
        "http-origin",
        "agent-bridge",
    ]


def test_owned_pair_refuses_preexisting_bridge_without_unregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)
    statuses["agent-bridge"] = "enabled"
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="unregistered Bridge"):
        lifecycle.start_owned_pair(_id(), tmp_path / "operator.toml", 19283, home=tmp_path)
    assert not any(operation in {"register", "unregister"} for operation, _ in calls)
    assert statuses["agent-bridge"] == "enabled"


def test_owned_bridge_start_refuses_stale_socket_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, calls, sock = _fixture(tmp_path, monkeypatch)
    sock.write_bytes(b"stale")
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="occupied"):
        lifecycle.start_owned_bridge(_id(), tmp_path / "operator.toml", 19283, home=tmp_path)
    assert not any(operation == "register" for operation, _ in calls)


def test_owned_bridge_registration_requires_approval_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, calls, _ = _fixture(tmp_path, monkeypatch)
    original = lifecycle._manager

    def manager(identity, operation, component, *, home=None):
        if operation == "register" and component == "agent-bridge":
            calls.append((operation, component))
            return "requires-approval"
        return original(identity, operation, component, home=home)

    monkeypatch.setattr(lifecycle, "_manager", manager)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="readiness failed"):
        lifecycle.start_owned_bridge(_id(), tmp_path / "operator.toml", 19283, home=tmp_path)
    assert ("unregister", "agent-bridge") in calls


def test_owned_stop_refuses_bridge_while_core_still_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)
    statuses.update({"agent-bridge": "enabled", "http-origin": "enabled"})
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="stop Core before"):
        lifecycle.stop_owned_bridge(_id(), home=tmp_path)
    assert not any(operation == "unregister" for operation, _ in calls)


@pytest.mark.parametrize("connector", ("cloudflare", "tunnel"))
def test_owned_stop_refuses_live_connector_before_unregistration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connector: str
) -> None:
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)
    statuses.update({"agent-bridge": "enabled", "http-origin": "enabled", connector: "enabled"})
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        lifecycle.stop_owned_core(_id(), home=tmp_path)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        lifecycle.stop_owned_pair(_id(), home=tmp_path)
    assert not any(operation == "unregister" for operation, _ in calls)

    # Even an already-unregistered Core does not authorize stopping the Bridge
    # while an independently registered ingress Connector still exists.
    statuses["http-origin"] = "not-registered"
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        lifecycle.stop_owned_bridge(_id(), home=tmp_path)
    assert not any(operation == "unregister" for operation, _ in calls)


def test_owned_stop_pair_orders_core_before_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    statuses, calls, _ = _fixture(tmp_path, monkeypatch)
    statuses.update({"agent-bridge": "enabled", "http-origin": "enabled"})
    assert lifecycle.stop_owned_pair(_id(), home=tmp_path)["state"] == "not-registered"
    assert [component for operation, component in calls if operation == "unregister"] == [
        "http-origin",
        "agent-bridge",
    ]


def test_owned_bridge_readiness_checks_socket_mode_owner_and_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # macOS sun_path is ~104 bytes; pytest's long generated paths are
    # unsuitable for testing the real AF_UNIX socket mode.
    short_dir = Path(tempfile.mkdtemp(prefix="sf25-", dir="/tmp"))
    socket_path = short_dir / "bridge.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        monkeypatch.setattr(lifecycle.darwin_service, "_launchd_job_pid", lambda _: 4111)

        async def rpc(_path):
            return True

        monkeypatch.setattr(lifecycle, "_probe_bridge_ipc", rpc)
        with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="not ready"):
            lifecycle._bridge_ready(_id(), socket_path, timeout=0.01)
        os.chmod(socket_path, 0o600)
        assert lifecycle._bridge_ready(_id(), socket_path, timeout=0.1) == 4111
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        short_dir.rmdir()


def test_owned_lifecycle_cli_routes_to_independent_identity_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    invoked: list[tuple[str, str, int | None]] = []

    def fake_start(identity, config, port):
        invoked.append(("start", identity.bridge_mode, port))
        assert identity.instance_id == identity.bridge_id == "sandbox"
        assert config == tmp_path / "serverfs.toml"
        return {"instance_id": identity.instance_id, "state": "enabled"}

    def fake_stop(identity):
        invoked.append(("stop", identity.bridge_mode, None))
        assert identity.instance_id == identity.bridge_id == "sandbox"
        return {"instance_id": identity.instance_id, "state": "not-registered"}

    monkeypatch.setattr(lifecycle, "start_owned_pair", fake_start)
    monkeypatch.setattr(lifecycle, "stop_owned_pair", fake_stop)
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "sandbox",
                "owned-start",
                "--config",
                str(tmp_path / "serverfs.toml"),
                "--http-port",
                "19333",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "enabled"
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "sandbox",
                "owned-stop",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "not-registered"
    assert invoked == [("start", "owned", 19333), ("stop", "owned", None)]


def test_native_manager_allows_named_owned_bridge_but_refuses_file_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = []

    def fake_run(argv, **_kwargs):
        invoked.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "services": {"agent-bridge": "not-registered"},
                }
            ),
        )

    monkeypatch.setattr(
        runtime, "_manager_path", lambda *_args, **_kwargs: Path("/synthetic/manager")
    )
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    assert runtime._manager(_id(), "status", "agent-bridge") == "not-registered"
    assert invoked == [["/synthetic/manager", "status", "agent-bridge"]]
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="invalid"):
        runtime._manager(
            InstanceIdentity(instance_id="files", bridge_mode="disabled"),
            "status",
            "agent-bridge",
        )
    with pytest.raises(runtime.DarwinInstanceRuntimeError, match="invalid"):
        runtime._manager(InstanceIdentity(), "register", "agent-bridge")
    assert len(invoked) == 1


def test_owned_unstage_cli_never_routes_to_file_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from serverfs_mcp import darwin_instance_owned_unstage as owned_unstage

    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed = []

    def fake_unstage(identity):
        observed.append((identity.instance_id, identity.bridge_mode, identity.bridge_id))
        return {"instance_id": identity.instance_id, "state": "unstaged"}

    monkeypatch.setattr(owned_unstage, "unstage_owned_candidate", fake_unstage)
    assert darwin_instance_cli.main(["--instance", "owned-test", "owned-unstage"]) == 0
    assert observed == [("owned-test", "owned", "owned-test")]
    assert json.loads(capsys.readouterr().out)["state"] == "unstaged"


def test_owned_status_is_scoped_and_never_mutates_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls, _ = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": "owned-test",
                "bridge_mode": "owned",
                "bridge_id": "owned-test",
                "core": {"port": 19283},
                "services": {"http-origin": {}, "agent-bridge": {}, "cloudflare": {}, "tunnel": {}},
            }
        )
    )
    manifest.chmod(0o600)
    labels: list[str] = []
    monkeypatch.setattr(
        lifecycle.darwin_service,
        "_launchd_job_pid",
        lambda label: labels.append(label) or None,
    )
    result = lifecycle.owned_instance_status(_id(), home=tmp_path)
    assert result == {
        "instance_id": "owned-test",
        "bridge_mode": "owned",
        "core_port": 19283,
        "registrations": {
            "bridge": "not-registered",
            "core": "not-registered",
            "cloudflare": "not-found",
            "openai": "not-found",
        },
        "pids": {"bridge": None, "core": None, "cloudflare": None, "openai": None},
        "bridge_socket_private": False,
    }
    assert all(operation == "status" for operation, _ in calls)
    assert [(operation, component) for operation, component in calls] == [
        ("status", "agent-bridge"),
        ("status", "http-origin"),
        ("status", "cloudflare"),
        ("status", "tunnel"),
    ]


@pytest.mark.parametrize(
    ("service", "provider", "connector_id"),
    [("cloudflare.blue", "cloudflare", "blue"), ("tunnel.blue", "openai", "blue")],
)
def test_owned_status_reports_dynamic_connector_registration_and_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    provider: str,
    connector_id: str,
) -> None:
    _, calls, _ = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": "owned-test",
                "bridge_mode": "owned",
                "bridge_id": "owned-test",
                "core": {"port": 19283},
                "services": {"http-origin": {}, "agent-bridge": {}, service: {}},
            }
        )
    )
    manifest.chmod(0o600)
    status_calls: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_manager",
        lambda _identity, operation, key, **_kwargs: status_calls.append(key) or "not-registered",
    )
    labels: list[str] = []
    monkeypatch.setattr(
        lifecycle.darwin_service,
        "_launchd_job_pid",
        lambda label: (
            labels.append(label)
            or (7331 if label == _id().macos_label(provider, connector_id) else None)
        ),
    )
    result = lifecycle.owned_instance_status(_id(), home=tmp_path)
    assert result["registrations"][service] == "not-registered"
    assert result["pids"][service] == 7331
    assert service in status_calls
    assert _id().macos_label(provider, connector_id) in labels


@pytest.mark.parametrize("service", ["cloudflare.blue", "tunnel.blue"])
def test_owned_stop_refuses_registered_dynamic_connector_before_any_unregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    _, calls, _ = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": "owned-test",
                "bridge_mode": "owned",
                "bridge_id": "owned-test",
                "core": {"port": 19283},
                "services": {"http-origin": {}, "agent-bridge": {}, service: {}},
            }
        )
    )
    manifest.chmod(0o600)
    statuses = {"agent-bridge": "enabled", "http-origin": "enabled", service: "enabled"}
    original = lifecycle._manager

    def manager(identity, operation, component, *, home=None):
        if operation == "status" and component in statuses:
            calls.append((operation, component))
            return statuses[component]
        return original(identity, operation, component, home=home)

    monkeypatch.setattr(lifecycle, "_manager", manager)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        lifecycle.stop_owned_pair(_id(), home=tmp_path)
    assert not any(operation == "unregister" for operation, _ in calls)


@pytest.mark.parametrize(
    ("service", "provider", "connector_id"),
    [
        ("cloudflare", "cloudflare", None),
        ("cloudflare.blue", "cloudflare", "blue"),
        ("tunnel.green", "openai", "green"),
    ],
)
@pytest.mark.parametrize(
    "stop", (lifecycle.stop_owned_core, lifecycle.stop_owned_bridge, lifecycle.stop_owned_pair)
)
def test_owned_stop_refuses_live_connector_pid_when_manager_status_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    provider: str,
    connector_id: str | None,
    stop,
) -> None:
    _, calls, _ = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    manifest = root / "services.json"
    document = json.loads(manifest.read_text())
    document["services"][service] = {}
    manifest.write_text(json.dumps(document))
    manifest.chmod(0o600)
    pid_label = _id().macos_label(provider, connector_id)
    pid_lookups: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_manager",
        lambda _identity, operation, key, **_kwargs: (
            calls.append((operation, key)) or "not-registered"
        ),
    )
    monkeypatch.setattr(
        lifecycle.darwin_service,
        "_launchd_job_pid",
        lambda label: pid_lookups.append(label) or (7331 if label == pid_label else None),
    )

    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="Connector"):
        stop(_id(), home=tmp_path)

    assert pid_label in pid_lookups
    assert not any(operation == "unregister" for operation, _ in calls)


@pytest.mark.parametrize("service", ["cloudflare..blue", "tunnel.BLUE", "unknown.blue"])
def test_owned_status_refuses_malformed_or_unknown_manifest_service_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    _fixture(tmp_path, monkeypatch)
    root = tmp_path / "Library/Application Support/ServerFS/instances/owned-test/service-manager"
    manifest = root / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "v0.25-named-owned-core",
                "instance_id": "owned-test",
                "bridge_mode": "owned",
                "bridge_id": "owned-test",
                "core": {"port": 19283},
                "services": {"http-origin": {}, "agent-bridge": {}, service: {}},
            }
        )
    )
    manifest.chmod(0o600)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="untrusted"):
        lifecycle.owned_instance_status(_id(), home=tmp_path)


def test_owned_status_cli_does_not_infer_file_only_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")

    def fake_status(identity):
        assert identity.bridge_mode == "owned"
        assert identity.bridge_id == identity.instance_id == "sandbox"
        return {"instance_id": "sandbox", "bridge_mode": "owned"}

    monkeypatch.setattr(lifecycle, "owned_instance_status", fake_status)
    assert darwin_instance_cli.main(["--instance", "sandbox", "owned-status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"instance_id": "sandbox", "bridge_mode": "owned"}


def test_owned_lifecycle_never_addresses_default_or_shared(tmp_path: Path) -> None:
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="independently named"):
        lifecycle.stop_owned_pair(InstanceIdentity(instance_id="default"), home=tmp_path)
    with pytest.raises(lifecycle.DarwinOwnedLifecycleError, match="independently named"):
        lifecycle.stop_owned_pair(
            InstanceIdentity(
                instance_id="shared-client", bridge_mode="shared", bridge_id="default"
            ),
            home=tmp_path,
        )
