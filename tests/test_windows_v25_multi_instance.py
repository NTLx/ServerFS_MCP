"""Windows v25 I1 instance-scoped Core contracts; no real OS/service mutations."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.windows_v25 import cli, readiness, runtime, storage, tasks
from serverfs_mcp.windows_v25.spec import Manifest, ManifestError


def core(tmp_path: Path, name: str = "staging", port: int = 18868) -> Manifest:
    return Manifest.parse(
        {
            "schema": 2,
            "instance_id": name,
            "component": "core",
            "sid": "S-1-5-21-111-222-333-1001",
            "pythonw": str(
                tmp_path / "v25" / "instances" / name / "envs" / "core" / "Scripts" / "pythonw.exe"
            ),
            "settings": {
                "config": str(tmp_path / "policy" / name / "serverfs.toml"),
                "auth_file": str(tmp_path / "private" / name / "core.env"),
                "port": port,
                "hostname": "core.example.net",
            },
        }
    )


def test_two_cores_have_distinct_manifest_tasks_and_entrypoints(tmp_path: Path) -> None:
    a, b = core(tmp_path), core(tmp_path, "production", 18869)
    assert Manifest.parse(a.to_dict()) == a
    assert a.schema == b.schema == 2
    assert a.task_name() == "ServerFS v25 staging Core"
    assert a.task_name() != b.task_name()
    assert a.task_argv()[-2:] == ("core", "staging")
    assert b.task_argv()[-2:] == ("core", "production")
    assert a.settings["port"] != b.settings["port"]


@pytest.mark.parametrize("name", ["", "../x", "A", "a_b", "a" * 33, "default"])
def test_invalid_instance_id_refused(tmp_path: Path, name: str) -> None:
    raw = core(tmp_path).to_dict()
    raw["instance_id"] = name
    with pytest.raises(ManifestError):
        Manifest.parse(raw)


def test_v2_requires_strict_schema_and_supports_independent_bridge(tmp_path: Path) -> None:
    raw = core(tmp_path).to_dict()
    raw["schema"] = 1
    with pytest.raises(ManifestError):
        Manifest.parse(raw)
    raw["schema"] = 2
    del raw["instance_id"]
    with pytest.raises(ManifestError):
        Manifest.parse(raw)
    raw = core(tmp_path).to_dict()
    raw["component"] = "bridge"
    raw["settings"] = {
        "config": str(tmp_path / "policy" / "serverfs.toml"),
        "bridge_python": str(tmp_path / "envs" / "bridge" / "Scripts" / "python.exe"),
    }
    raw["pythonw"] = str(tmp_path / "v25/instances/staging/envs/bridge/Scripts/pythonw.exe")
    bridge = Manifest.parse(raw)
    assert bridge.task_name() == "ServerFS v25 staging Agent Bridge"
    assert bridge.task_argv()[-2:] == ("bridge", "staging")
    assert Manifest.parse(bridge.to_dict()) == bridge


def test_default_v1_bytes_and_start_arguments_unchanged(tmp_path: Path) -> None:
    raw = core(tmp_path).to_dict()
    raw["schema"] = 1
    del raw["instance_id"]
    v1 = Manifest.parse(raw)
    assert v1.task_name() == "ServerFS v25 Core"
    assert v1.task_argv()[-1] == "core"
    assert "instance_id" not in v1.to_dict()


def test_storage_paths_and_scheduler_xml_are_isolated(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    a, b = core(tmp_path), core(tmp_path, "production")
    assert storage.root("staging") == tmp_path / "ServerFS/v25/instances/staging"
    assert storage.home("core", "staging") != storage.home("core", "production")
    assert tasks._task_spec(a).executable == storage.pinned_pythonw("core", "staging")
    assert tasks._task_spec(b).name != tasks._task_spec(a).name
    assert tasks._task_spec(a).arguments[-1] == "staging"


def test_private_prepare_never_creates_default_or_other_instance(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_mcp import native_lifecycle, windows_private_artifacts

    appdata = tmp_path / "LocalAppData"
    appdata.mkdir()
    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "S-1-5-21-111-222-333-1001")
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: appdata, raising=False)
    protected: list[Path] = []
    monkeypatch.setattr(
        windows_private_artifacts,
        "protect_new_file",
        lambda path, sid: protected.append(path),
    )
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)
    storage.prepare("core", "staging")
    base = appdata / "ServerFS/v25"
    assert base / "instances/staging/services/core" in protected
    assert base / "instances/staging/envs/core" in protected
    assert not (base / "services/core").exists()
    assert not (base / "instances/production").exists()
    storage.prepare("core", "staging")
    assert len(set(protected)) == len(protected)


def test_manifest_load_rejects_cross_instance_substitution(tmp_path: Path, monkeypatch) -> None:
    from serverfs_mcp import native_lifecycle

    monkeypatch.setattr(storage, "_windows", lambda: None)
    monkeypatch.setattr(storage, "_sid", lambda: "S-1-5-21-111-222-333-1001")
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)
    raw = core(tmp_path).to_dict()
    raw["pythonw"] = str(storage.pinned_pythonw("core", "staging"))
    path = storage.home("core", "staging") / "service.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert storage.load("core", "staging").instance_id == "staging"
    raw["instance_id"] = "production"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(storage.StorageError, match="service manifest content is invalid"):
        storage.load("core", "staging")


def test_cli_preflight_requires_private_binding_when_bridge_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    from serverfs_mcp import native_config
    from serverfs_mcp.windows_v25.core_bridge_binding import CoreBridgeBindingError

    value = core(tmp_path)
    monkeypatch.setattr(storage, "home", lambda *_args: tmp_path)
    monkeypatch.setattr(runtime, "_core_configuration", lambda manifest: (tmp_path, {}))
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda config: ([], SimpleNamespace(bridge_required=False)),
    )
    cli._preflight(value)
    monkeypatch.setattr(
        native_config,
        "load_native_config",
        lambda config: ([], SimpleNamespace(bridge_required=True)),
    )
    with pytest.raises(CoreBridgeBindingError, match="invalid or not private"):
        cli._preflight(value)


def test_cli_instance_routing_does_not_touch_default(monkeypatch, tmp_path: Path) -> None:
    changes: list[tuple[str, str]] = []
    monkeypatch.setattr(storage, "prepare", lambda comp, ident: changes.append((comp, ident)))
    prepared = cli.parser().parse_args(["core", "prepare", "--instance", "staging"])
    assert cli.dispatch(prepared)["prepared"] is True
    assert changes == [("core", "staging")]
    value = core(tmp_path)
    monkeypatch.setattr(storage, "load", lambda comp, ident: value)

    class FakeScheduler:
        def status(self, manifest):
            return {"instance_id": manifest.instance_id}

    monkeypatch.setattr(cli, "Scheduler", FakeScheduler)
    requested = cli.parser().parse_args(["core", "status", "--instance", "staging"])
    assert cli.dispatch(requested)["instance_id"] == "staging"


def test_nondefault_bridge_status_reads_only_its_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = core(tmp_path).to_dict()
    raw["component"] = "bridge"
    raw["pythonw"] = str(tmp_path / "v25/instances/staging/envs/bridge/Scripts/pythonw.exe")
    raw["settings"] = {
        "config": str(tmp_path / "policy/staging/serverfs.toml"),
        "bridge_python": str(tmp_path / "envs/bridge/Scripts/python.exe"),
    }
    expected = Manifest.parse(raw)
    seen = []

    def load(component: str, instance: str) -> Manifest:
        seen.append((component, instance))
        return expected

    monkeypatch.setattr(storage, "load", load)
    monkeypatch.setattr(
        tasks.Scheduler,
        "status",
        lambda self, manifest: {"task_name": manifest.task_name(), "registered": False},
    )
    args = cli.parser().parse_args(["bridge", "status", "--instance", "staging"])
    result = cli.dispatch(args)
    assert result["task_name"] == "ServerFS v25 staging Agent Bridge"
    assert seen == [("bridge", "staging")]


def test_invalid_cli_instance_refused_before_storage(monkeypatch) -> None:
    monkeypatch.setattr(storage, "load", lambda *args: pytest.fail("unexpected load"))
    args = cli.parser().parse_args(["core", "status", "--instance", "../escape"])
    with pytest.raises(ManifestError, match="invalid service instance"):
        cli.dispatch(args)


def test_runtime_routes_nondefault_core_without_os_operations(tmp_path, monkeypatch) -> None:
    value = core(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_verified_owner",
        lambda component, ident: seen.append(ident) or value,
    )
    monkeypatch.setattr(runtime, "run_core", lambda manifest: 0 if manifest is value else 1)
    assert runtime.main(["core", "staging"]) == 0
    assert seen == ["staging"]


def test_runtime_identity_state_paths_are_private_to_instance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        storage,
        "home",
        lambda comp, ident: tmp_path / ident / comp,
    )
    assert readiness.identity_path("core", "staging") != (
        readiness.identity_path("core", "production")
    )


def test_runtime_pid_proof_cannot_be_replayed_between_instances(tmp_path, monkeypatch) -> None:
    from serverfs_mcp import windows_process

    instance = core(tmp_path)
    identity = tmp_path / "identity.json"
    image = tmp_path / "pythonw.exe"
    proof = SimpleNamespace(pid=1234, creation_time=6789, image_path=image)
    monkeypatch.setattr(readiness, "identity_path", lambda comp, name: identity)
    monkeypatch.setattr(storage, "check_private", lambda *_args, **_kw: None)
    monkeypatch.setattr(windows_process, "process_identity", lambda pid: proof)
    saved = {
        "schema": 2,
        "instance_id": "staging",
        "component": "core",
        "pid": 1234,
        "created": 6789,
        "image": str(image),
    }
    identity.write_text(json.dumps(saved), encoding="utf-8")
    assert readiness._current_identity(instance) is proof
    saved["instance_id"] = "production"
    identity.write_text(json.dumps(saved), encoding="utf-8")
    assert readiness._current_identity(instance) is None
