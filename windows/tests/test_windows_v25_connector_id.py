"""Windows v0.25 source-only multiple Cloudflare Connectors per Core.

Synthetic file paths and schemas only. Never installs/starts Windows tasks,
opens a real tunnel, reads credentials, touches production, or uses network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverfs_mcp import native_endpoint
from serverfs_mcp.windows_v25 import cli, readiness, runtime, storage, tasks
from serverfs_mcp.windows_v25.isolation import (
    InstanceCollisionError,
    assert_connector_isolation,
)
from serverfs_mcp.windows_v25.spec import Manifest, ManifestError

_SID = "S-1-5-21-111-222-333-1001"


def _connector(
    root: Path,
    connector_id: str | None,
    *,
    token: str | None = None,
) -> Manifest:
    instance = "staging"
    base = root / "instances" / instance
    raw = {
        "schema": 2,
        "instance_id": instance,
        "component": "cloudflare",
        "sid": _SID,
        "pythonw": str(base / "envs/cloudflare/Scripts/pythonw.exe"),
        "settings": {
            "cloudflared": str(base / "bin/cloudflared.exe"),
            "token_file": str(base / "secrets" / (token or f"{connector_id or 'primary'}.token")),
            "port": 18879,
            "hostname": "core.example.test",
        },
    }
    if connector_id is not None:
        raw["connector_id"] = connector_id
    return Manifest.parse(raw)


def test_two_cloudflare_connectors_share_only_core_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        storage,
        "pinned_pythonw",
        lambda component, instance: tmp_path / "envs" / component / "pythonw.exe",
    )
    first = _connector(tmp_path, "edge-a")
    second = _connector(tmp_path, "edge-b")
    primary = _connector(tmp_path, None)

    assert first != second != primary
    assert first.task_name() == "ServerFS v25 staging Cloudflare Connector edge-a"
    assert second.task_name() == "ServerFS v25 staging Cloudflare Connector edge-b"
    assert primary.task_name() == "ServerFS v25 staging Cloudflare Connector"
    assert first.task_argv()[-3:] == ("cloudflare", "staging", "edge-a")
    assert second.task_argv()[-3:] == ("cloudflare", "staging", "edge-b")
    assert primary.task_argv()[-2:] == ("cloudflare", "staging")
    assert first.pythonw == second.pythonw == primary.pythonw
    assert Manifest.parse(first.to_dict()) == first
    assert Manifest.parse(second.to_dict()) == second
    assert "connector_id" not in primary.to_dict()
    assert_connector_isolation(first, (first, second, primary))
    assert_connector_isolation(second, (first, second, primary))
    assert first.task_name() != second.task_name()
    assert tasks._task_spec(first).name == first.task_name()
    assert tasks._task_spec(second).arguments == second.task_argv()


@pytest.mark.parametrize(
    "bad",
    ["", "A", "edge_1", "../x", "east/west", "x" * 33, None, 123],
)
def test_connector_id_bad_shape_is_fail_closed(tmp_path: Path, bad: object) -> None:
    valid = _connector(tmp_path, "edge")
    raw = valid.to_dict()
    raw["connector_id"] = bad
    with pytest.raises(ManifestError):
        Manifest.parse(raw)


@pytest.mark.parametrize("component", ("core", "bridge"))
def test_non_ingress_manifest_cannot_claim_connector_id(tmp_path: Path, component: str) -> None:
    data = _connector(tmp_path, "edge").to_dict()
    data["component"] = component
    if component == "core":
        data["settings"] = {
            "config": str(tmp_path / "policy.toml"),
            "auth_file": str(tmp_path / "core.env"),
            "port": 18879,
            "hostname": "core.example.test",
        }
    else:
        data["settings"] = {
            "config": str(tmp_path / "policy.toml"),
            "bridge_python": str(tmp_path / "bridge/python.exe"),
        }
    with pytest.raises(ManifestError, match="only independent connectors"):
        Manifest.parse(data)


def test_colliding_same_connector_id_and_duplicate_secrets_rejected(
    tmp_path: Path,
) -> None:
    edge = _connector(tmp_path, "edge")
    changed = _connector(tmp_path, "edge", token="different.token")
    with pytest.raises(InstanceCollisionError, match="identity"):
        assert_connector_isolation(changed, [edge])
    reused = _connector(tmp_path, "second", token="edge.token")
    with pytest.raises(InstanceCollisionError, match="credential"):
        assert_connector_isolation(reused, [edge])
    assert_connector_isolation(_connector(tmp_path, "second"), [edge])


def test_storage_inventory_load_and_identity_paths_are_connector_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "ServerFS" / "v25"

    def root(instance_id: str = "default") -> Path:
        return base if instance_id == "default" else base / "instances" / instance_id

    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "root", root)
    monkeypatch.setattr(storage, "_sid", lambda: _SID)

    def check(path: Path, *, directory: bool, sid: str | None = None) -> None:
        assert sid == _SID
        if not (path.is_dir() if directory else path.is_file()):
            raise storage.StorageError("synthetic private path is absent")

    monkeypatch.setattr(storage, "check_private", check)
    (base / "services").mkdir(parents=True)
    pair = (_connector(base, "edge-a"), _connector(base, "edge-b"))
    for manifest in pair:
        dest = storage.home(manifest.component, manifest.instance_id, manifest.connector_id)
        dest.mkdir(parents=True)
        (dest / "service.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        assert storage.load("cloudflare", "staging", manifest.connector_id) == manifest
        assert (
            readiness.identity_path("cloudflare", "staging", manifest.connector_id)
            == dest / "identity.json"
        )
    inventory = storage.component_inventory("cloudflare")
    assert {item.connector_id for item in inventory} == {"edge-a", "edge-b"}
    assert {item.task_name() for item in inventory} == {item.task_name() for item in pair}
    with pytest.raises((storage.StorageError, FileNotFoundError)):
        storage.load("cloudflare", "staging", "missing")


def test_cli_routes_exact_connector_lifecycle_and_rejects_invalid_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _connector(tmp_path, "edge-a")
    selected: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        storage,
        "prepare",
        lambda component, instance, connector_id: selected.append(
            (component, instance, connector_id)
        ),
    )
    assert cli.dispatch(
        cli.parser().parse_args(
            ["cloudflare", "--instance", "staging", "--connector-id", "edge-a", "prepare"]
        )
    )["prepared"]
    assert selected == [("cloudflare", "staging", "edge-a")]

    def load(component, instance, connector_id):
        selected.append((component, instance, connector_id))
        assert component == "cloudflare" and instance == "staging"
        return target

    monkeypatch.setattr(storage, "load", load)
    monkeypatch.setattr(
        tasks.Scheduler,
        "status",
        lambda self, manifest: {"task_name": manifest.task_name(), "registered": False},
    )
    status = cli.dispatch(
        cli.parser().parse_args(
            ["cloudflare", "--instance", "staging", "--connector-id", "edge-a", "status"]
        )
    )
    assert status["task_name"] == target.task_name()
    assert selected[-1] == ("cloudflare", "staging", "edge-a")

    for argv in (
        ["core", "--instance", "staging", "--connector-id", "edge-a", "prepare"],
        ["cloudflare", "--connector-id", "edge-a", "prepare"],
        ["cloudflare", "--instance", "staging", "--connector-id", "UPPER", "prepare"],
    ):
        with pytest.raises(ManifestError):
            cli.dispatch(cli.parser().parse_args(argv))
    assert selected.count(("cloudflare", "staging", "edge-a")) == 2


def test_cli_configure_stages_connector_id_without_leaking_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _connector(tmp_path, "edge")
    monkeypatch.setattr(native_endpoint, "current_user_sid", lambda: _SID)
    monkeypatch.setattr(
        storage,
        "pinned_pythonw",
        lambda component, instance: Path(manifest.pythonw),
    )
    accepted = []
    monkeypatch.setattr(cli, "_preflight", lambda item: accepted.append(item))
    monkeypatch.setattr(cli, "_assert_instance_isolation", lambda item: accepted.append(item))
    monkeypatch.setattr(storage, "stage", lambda item: accepted.append(item))
    args = cli.parser().parse_args(
        [
            "cloudflare",
            "--instance",
            "staging",
            "--connector-id",
            "edge",
            "configure",
            "--cloudflared",
            str(manifest.settings["cloudflared"]),
            "--token-file",
            str(manifest.settings["token_file"]),
            "--port",
            "18879",
            "--hostname",
            "core.example.test",
        ]
    )
    assert cli.dispatch(args)["configured"]
    assert accepted == [manifest, manifest, manifest]
    assert manifest.task_arguments().endswith("cloudflare staging edge")
    assert "token" not in manifest.task_arguments()


def test_qualified_connector_lifecycle_never_targets_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every CLI transition operates on precisely one connector_id task."""
    edge_a = _connector(tmp_path, "edge-a")
    edge_b = _connector(tmp_path, "edge-b")
    manifests = {"edge-a": edge_a, "edge-b": edge_b}
    visited: list[tuple[str, str]] = []
    preflighted: list[str] = []
    collisions: list[str] = []

    def load(component: str, instance: str, connector_id: str) -> Manifest:
        assert component == "cloudflare" and instance == "staging"
        visited.append(("load", connector_id))
        return manifests[connector_id]

    class FakeScheduler:
        def _result(self, action: str, manifest: Manifest) -> dict:
            assert manifest.connector_id is not None
            visited.append((action, manifest.connector_id))
            return {"component": manifest.component, "task_name": manifest.task_name()}

        def install(self, manifest: Manifest) -> dict:
            return self._result("install", manifest)

        def status(self, manifest: Manifest) -> dict:
            return self._result("status", manifest)

        def start(self, manifest: Manifest) -> dict:
            return self._result("start", manifest)

        def stop(self, manifest: Manifest) -> dict:
            return self._result("stop", manifest)

        def uninstall(self, manifest: Manifest) -> dict:
            return self._result("uninstall", manifest)

    monkeypatch.setattr(storage, "load", load)
    monkeypatch.setattr(cli, "Scheduler", FakeScheduler)
    monkeypatch.setattr(
        cli, "_preflight", lambda manifest: preflighted.append(manifest.connector_id)
    )
    monkeypatch.setattr(
        cli,
        "_assert_instance_isolation",
        lambda manifest: collisions.append(manifest.connector_id),
    )

    for connector_id in ("edge-a", "edge-b"):
        for action in ("install", "status", "start", "restart", "stop", "uninstall"):
            args = cli.parser().parse_args(
                [
                    "cloudflare",
                    "--instance",
                    "staging",
                    "--connector-id",
                    connector_id,
                    action,
                ]
            )
            result = cli.dispatch(args)
            assert result["task_name"] == manifests[connector_id].task_name()
        peer = "edge-b" if connector_id == "edge-a" else "edge-a"
        # Nothing in this connector's transition interval can resolve a
        # manifest, stop a task, or publish readiness for the other ID.
        current = visited[:]
        if connector_id == "edge-a":
            assert all(identifier != peer for _, identifier in current)

    assert visited == [
        (action, connector_id)
        for connector_id in ("edge-a", "edge-b")
        for action in (
            "load",
            "install",
            "load",
            "status",
            "load",
            "start",
            "load",
            "stop",
            "start",
            "load",
            "stop",
            "load",
            "uninstall",
        )
    ]
    assert preflighted == ["edge-a"] * 3 + ["edge-b"] * 3
    assert collisions == ["edge-a"] * 3 + ["edge-b"] * 3


def test_runtime_main_dispatches_qualified_connector_without_default_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _connector(tmp_path, "edge")
    calls = []
    monkeypatch.setattr(
        runtime,
        "_verified_owner",
        lambda component, instance, connector_id: (
            calls.append((component, instance, connector_id)) or manifest
        ),
    )
    monkeypatch.setattr(runtime, "run_connector", lambda value: 0 if value is manifest else 2)
    assert runtime.main(["cloudflare", "staging", "edge"]) == 0
    assert calls == [("cloudflare", "staging", "edge")]
