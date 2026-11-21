"""Isolated v0.25 Bridge owner/runtime packaging and interpreter trust tests.

No real Task Scheduler, named Bridge Job, provider, or production process is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_bridge_control as control
from serverfs_mcp import windows_bridge_owner as owner
from serverfs_mcp import windows_bridge_service as service
from serverfs_mcp import windows_bridge_task_spec as contract

SID = "S-1-5-21-100-200-300-1000"


def _spec(tmp_path: Path) -> contract.BridgeTaskSpec:
    return contract.BridgeTaskSpec(
        SID,
        tmp_path / "ServerFS" / "bridge-owner-env" / "Scripts" / "pythonw.exe",
        tmp_path / "config.toml",
    )


def _manifest(spec: contract.BridgeTaskSpec) -> dict:
    return contract.build_manifest(spec, contract.build_task_xml(spec))


def test_manifest_pins_a_separate_bridge_runtime_and_rejects_any_override(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    manifest = _manifest(spec)
    assert manifest["bridge_executable"] == str(
        tmp_path / "ServerFS" / "bridge-runtime-env" / "Scripts" / "python.exe"
    )
    assert manifest["task_executable"] != manifest["bridge_executable"]
    assert contract.validate_manifest(manifest) == (True, "")
    changed = dict(manifest, bridge_executable=str(tmp_path / "attacker" / "python.exe"))
    assert contract.validate_manifest(changed)[0] is False
    missing = dict(manifest)
    del missing["bridge_executable"]
    assert contract.validate_manifest(missing)[0] is False
    assert "SERVERFS_BRIDGE_PYTHON" not in json.dumps(manifest)


def test_bridge_runtime_checks_private_directory_chain_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_lifecycle, windows_core_control

    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    observed: list[tuple[Path, bool, str]] = []

    def checked(path: Path, *, directory: bool, sid: str) -> None:
        observed.append((path, directory, sid))

    monkeypatch.setattr(windows_core_control, "_check_private", checked)
    spec = _spec(tmp_path)
    result = service._verified_bridge_interpreter(_manifest(spec))
    expected_root = tmp_path / "ServerFS" / "bridge-runtime-env"
    assert result == expected_root / "Scripts" / "python.exe"
    assert observed == [
        (expected_root, True, SID),
        (expected_root / "Scripts", True, SID),
        (expected_root / "Lib", True, SID),
        (expected_root / "Lib" / "site-packages", True, SID),
        (expected_root / "Lib" / "site-packages" / "serverfs_agent_bridge", True, SID),
        (result, False, SID),
    ]


def test_bridge_runtime_refuses_unsafe_acl_or_wrong_scope_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_lifecycle, windows_core_control

    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    spec = _spec(tmp_path)
    manifest = _manifest(spec)
    with pytest.raises(service.BridgeServiceError, match="pinned scope"):
        service._verified_bridge_interpreter(
            dict(manifest, bridge_executable=str(tmp_path / "other.exe"))
        )

    def denied(*_args: object, **_kwargs: object) -> None:
        raise OSError("sensitive-filename-marker")

    monkeypatch.setattr(windows_core_control, "_check_private", denied)
    with pytest.raises(service.BridgeServiceError, match="private installation") as err:
        service._verified_bridge_interpreter(manifest)
    assert "sensitive-filename-marker" not in str(err.value)


def test_owner_preflight_imports_bridge_only_in_separate_pinned_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import windows_service

    spec = _spec(tmp_path)
    bridge = spec.bridge_executable()
    native_calls: list[str] = []
    monkeypatch.setattr(
        windows_service, "_validate_runtime_backend", lambda: native_calls.append("owner")
    )
    monkeypatch.setattr(service, "_verified_bridge_interpreter", lambda _manifest: bridge)
    monkeypatch.setattr(
        service,
        "_runtime_environment",
        lambda: {"PATH": "controlled"},
    )
    observed: list[tuple[list[str], dict]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    control._ensure_backend(spec)
    assert native_calls == ["owner"]
    assert len(observed) == 1
    args, kwargs = observed[0]
    assert args == [str(bridge), "-I", "-B", "-c", "import serverfs_agent_bridge.main"]
    assert kwargs["env"] == {"PATH": "controlled"}
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 30


def test_missing_bridge_wheel_fails_closed_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import windows_service

    spec = _spec(tmp_path)
    monkeypatch.setattr(windows_service, "_validate_runtime_backend", lambda: None)
    monkeypatch.setattr(
        service, "_verified_bridge_interpreter", lambda _manifest: spec.bridge_executable()
    )
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    with pytest.raises(control.BridgeControlError, match="isolated Bridge runtime"):
        control._ensure_backend(spec)


def test_separate_bridge_renderer_uses_private_interpreter_and_scrubs_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import supervisor

    bridge = _spec(tmp_path).bridge_executable()
    monkeypatch.setenv("SERVERFS_BRIDGE_PYTHON", "C:/untrusted/legacy-python.exe")
    monkeypatch.setattr(
        supervisor,
        "_bridge_render_env",
        lambda: {"SERVERFS_BRIDGE_PYTHON": "C:/untrusted/legacy-python.exe", "PATH": "safe"},
    )
    observed: list[tuple[list[str], dict]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        observed.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "config_path": str(tmp_path / "private" / "bridge.json"),
                    "state_dir": str(tmp_path / "private"),
                    "lock_dir": str(tmp_path / "locks"),
                    "socket_path": str(tmp_path / "bridge.pipe"),
                    "allowed_peer_sid": SID,
                }
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    result = supervisor._render_bridge_config({"workdirs": []}, bridge_python=bridge)
    assert result.allowed_peer_sid == SID
    assert len(observed) == 1
    args, kwargs = observed[0]
    assert args == [
        str(bridge),
        "-I",
        "-B",
        "-m",
        "serverfs_agent_bridge.render_config",
    ]
    assert kwargs["env"] == {"PATH": "safe"}
    assert kwargs["input"] == b'{"workdirs": []}'
    assert "C:/untrusted" not in repr(args) + repr(kwargs["env"])

    # Historical legacy call shape stays unchanged when no pin is supplied.
    observed.clear()
    supervisor._render_bridge_config({"workdirs": []})
    assert observed[0][0] == [
        "C:/untrusted/legacy-python.exe",
        "-m",
        "serverfs_agent_bridge.render_config",
    ]


def test_owner_interpreter_must_be_private_and_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverfs_mcp import native_lifecycle, windows_core_control

    prefix = tmp_path / "ServerFS" / "bridge-owner-env"
    scripts = prefix / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"fixture")
    monkeypatch.setattr(native_lifecycle, "window_local_app_data", lambda: tmp_path, raising=False)
    monkeypatch.setattr(control.sys, "prefix", str(prefix))
    monkeypatch.setattr(control, "_current_sid", lambda: SID)
    observed: list[tuple[Path, bool, str]] = []

    def checked(path: Path, *, directory: bool, sid: str) -> None:
        observed.append((path, directory, sid))

    monkeypatch.setattr(windows_core_control, "_check_private", checked)
    assert control._dedicated_pythonw() == scripts / "pythonw.exe"
    assert observed[-1] == (scripts / "pythonw.exe", False, SID)
    assert (prefix / "Lib" / "site-packages" / "serverfs_mcp", True, SID) in observed


def test_pinned_owner_argv_ignores_old_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _spec(tmp_path).bridge_executable()
    config = tmp_path / "private" / "bridge.json"
    monkeypatch.setenv("SERVERFS_BRIDGE_PYTHON", "C:/untrusted/python.exe")
    assert owner._pinned_bridge_argv(config, bridge) == [
        str(bridge),
        "-I",
        "-B",
        "-m",
        "serverfs_agent_bridge.main",
        "--config",
        str(config),
        "--supervised",
    ]
    assert "SERVERFS_BRIDGE_PYTHON" not in service._runtime_environment(
        {"SERVERFS_BRIDGE_PYTHON": "injected"}
    )
    with pytest.raises(Exception, match="interpreter is invalid"):
        owner._pinned_bridge_argv(config, Path("relative/python.exe"))
