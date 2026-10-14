from __future__ import annotations

import importlib
import importlib.metadata
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from serverfs_mcp import windows_process, windows_service
from serverfs_mcp.windows_process import ProcessEntry, ProcessIdentity


@pytest.fixture()
def service_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    monkeypatch.setattr(windows_service, "_validate_persistent_runtime_location", lambda: None)
    monkeypatch.setattr(windows_service, "_validate_runtime_backend", lambda: None)
    config = tmp_path / "serverfs.toml"
    config.write_text("[[workdirs]]\nalias='repo'\npath='C:/repo'\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        "CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n"
        "SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:7897\n",
        encoding="utf-8",
    )
    runner = tmp_path / "pythonw.exe"
    runner.write_bytes(b"")
    key = tmp_path / "api-key"
    key.write_text("secret-sentinel", encoding="utf-8")
    return config, env, runner, key


def _xml_root(payload: bytes) -> ET.Element:
    return ET.fromstring(payload.decode("utf-16"))


def _text(root: ET.Element, path: str) -> str:
    ns = {"t": windows_service.TASK_XML_NAMESPACE}
    element = root.find(path, ns)
    assert element is not None and element.text is not None
    return element.text


def test_validate_persistent_runtime_location_accepts_connector_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "connector-env"
    runtime.mkdir()
    monkeypatch.setattr(windows_service, "_persistent_runtime_root", lambda: runtime.resolve())
    monkeypatch.setattr(windows_service.sys, "prefix", str(runtime))

    windows_service._validate_persistent_runtime_location()


def test_validate_persistent_runtime_location_rejects_development_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "connector-env"
    development = tmp_path / ".venv"
    runtime.mkdir()
    development.mkdir()
    monkeypatch.setattr(windows_service, "_persistent_runtime_root", lambda: runtime.resolve())
    monkeypatch.setattr(windows_service.sys, "prefix", str(development))

    with pytest.raises(
        windows_service.WindowsServiceError,
        match="must run from the current-user connector-env",
    ):
        windows_service._validate_persistent_runtime_location()


def test_validate_runtime_backend_rejects_missing_native_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_package(_name: str) -> object:
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", missing_package)

    with pytest.raises(
        windows_service.WindowsServiceError,
        match="serverfs-windows-native is not installed",
    ):
        windows_service._validate_runtime_backend()


def test_validate_runtime_backend_rejects_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib, "import_module", lambda _name: object())
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.0.0-sentinel")

    with pytest.raises(
        windows_service.WindowsServiceError,
        match="ServerFS/native version mismatch",
    ):
        windows_service._validate_runtime_backend()


def test_task_xml_is_current_user_least_privilege_and_has_native_recovery(tmp_path: Path) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    runner = tmp_path / "pythonw.exe"
    sid = "S-1-5-21-111-222-333-1001"

    payload = windows_service.build_task_xml(
        user_sid=sid,
        task_executable=runner,
        config_path=config,
        env_file=env,
        start_boundary="2026-10-13T22:00:00+08:00",
    )
    root = _xml_root(payload)

    assert _text(root, ".//t:Principal/t:UserId") == sid
    assert _text(root, ".//t:Principal/t:LogonType") == "InteractiveToken"
    assert _text(root, ".//t:Principal/t:RunLevel") == "LeastPrivilege"
    assert _text(root, ".//t:LogonTrigger/t:UserId") == sid
    assert _text(root, ".//t:LogonTrigger/t:Enabled") == "true"
    ns = {"t": windows_service.TASK_XML_NAMESPACE}
    assert root.find(".//t:LogonTrigger/t:Repetition", ns) is None
    assert _text(root, ".//t:TimeTrigger/t:StartBoundary") == "2026-10-13T22:00:00+08:00"
    assert _text(root, ".//t:TimeTrigger/t:Enabled") == "true"
    assert _text(root, ".//t:TimeTrigger/t:Repetition/t:Interval") == "PT1M"
    assert root.find(".//t:TimeTrigger/t:Repetition/t:Duration", ns) is None
    assert _text(root, ".//t:TimeTrigger/t:Repetition/t:StopAtDurationEnd") == "false"
    assert _text(root, ".//t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert _text(root, ".//t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert _text(root, ".//t:RestartOnFailure/t:Interval") == "PT1M"
    assert _text(root, ".//t:RestartOnFailure/t:Count") == "255"
    task_arguments = _text(root, ".//t:Actions/t:Exec/t:Arguments")
    assert "_watchdog" in task_arguments
    assert " _run " not in task_arguments

    xml = payload.decode("utf-16")
    assert "HighestAvailable" not in xml
    assert "Password" not in xml
    assert "S4U" not in xml
    assert "SYSTEM" not in xml
    assert "LocalSystem" not in xml
    assert "HKLM" not in xml
    assert "runas" not in xml.lower()


def test_task_xml_contains_no_env_secret_values(tmp_path: Path) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    env.write_text("CONTROL_PLANE_API_KEY=secret-sentinel\n", encoding="utf-8")
    runner = tmp_path / "pythonw.exe"

    payload = windows_service.build_task_xml(
        user_sid="S-1-5-21-1-2-3-1001",
        task_executable=runner,
        config_path=config,
        env_file=env,
    )

    text = payload.decode("utf-16")
    assert "secret-sentinel" not in text
    assert str(env.resolve()) in text


def test_registered_task_validator_detects_security_drift(tmp_path: Path) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    runner = tmp_path / "pythonw.exe"
    sid = "S-1-5-21-1-2-3-1001"
    payload = windows_service.build_task_xml(
        user_sid=sid,
        task_executable=runner,
        config_path=config,
        env_file=env,
    )
    manifest = {
        "user_sid": sid,
        "task_executable": str(runner.resolve()),
        "task_arguments": windows_service._watchdog_argv(config, env),
        "config_path": str(config.resolve()),
    }

    valid, detail = windows_service.validate_registered_task(payload.decode("utf-16"), manifest)
    assert valid is True
    assert detail == ""

    drifted = payload.decode("utf-16").replace("LeastPrivilege", "HighestAvailable")
    valid, detail = windows_service.validate_registered_task(drifted, manifest)
    assert valid is False
    assert "principal run level" in detail

    drifted = payload.decode("utf-16").replace("<Interval>PT1M</Interval>", "", 1)
    valid, detail = windows_service.validate_registered_task(drifted, manifest)
    assert valid is False
    assert "heartbeat repetition interval" in detail


def test_registered_task_validator_accepts_scheduler_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    runner = tmp_path / "pythonw.exe"
    sid = "S-1-5-21-1-2-3-1001"
    payload = windows_service.build_task_xml(
        user_sid=sid,
        task_executable=runner,
        config_path=config,
        env_file=env,
    ).decode("utf-16")
    # Windows 11 canonicalizes the registered XML by dropping default-valued
    # RunLevel/trigger Enabled and may spell LogonTrigger.UserId as account name.
    payload = payload.replace("<RunLevel>LeastPrivilege</RunLevel>", "")
    payload = payload.replace("<Enabled>true</Enabled><UserId>", "<UserId>", 1)
    payload = payload.replace(
        f"<UserId>{sid}</UserId>",
        "<UserId>NTLXWORK\\lx</UserId>",
        1,
    )
    manifest = {
        "user_sid": sid,
        "task_executable": str(runner.resolve()),
        "task_arguments": windows_service._watchdog_argv(config, env),
        "config_path": str(config.resolve()),
    }
    monkeypatch.setattr(
        windows_service,
        "_identity_matches_sid",
        lambda identity, expected: identity in {sid, "NTLXWORK\\lx"} and expected == sid,
    )

    valid, detail = windows_service.validate_registered_task(payload, manifest)

    assert valid is True
    assert detail == ""


def test_registered_task_validator_rejects_different_trigger_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "serverfs.toml"
    env = tmp_path / ".env"
    runner = tmp_path / "pythonw.exe"
    sid = "S-1-5-21-1-2-3-1001"
    payload = windows_service.build_task_xml(
        user_sid=sid,
        task_executable=runner,
        config_path=config,
        env_file=env,
    ).decode("utf-16")
    payload = payload.replace(
        f"<UserId>{sid}</UserId>",
        "<UserId>OTHER\\user</UserId>",
        1,
    )
    manifest = {
        "user_sid": sid,
        "task_executable": str(runner.resolve()),
        "task_arguments": windows_service._watchdog_argv(config, env),
        "config_path": str(config.resolve()),
    }
    monkeypatch.setattr(
        windows_service,
        "_identity_matches_sid",
        lambda identity, expected: identity == sid and expected == sid,
    )

    valid, detail = windows_service.validate_registered_task(payload, manifest)

    assert valid is False
    assert "logon trigger user" in detail


def test_query_task_xml_uses_official_query_shape_and_hresult(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    xml = (
        '<?xml version="1.0"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" />'
    )

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=xml.encode("utf-16"), stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)

    assert windows_service._query_task_xml() is not None
    assert calls == [("/Query", "/TN", windows_service.TASK_NAME, "/XML", "/HResult")]
    assert "ONE" not in calls[0]


def test_query_task_xml_retries_then_reports_explicit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(windows_service.time, "sleep", lambda _seconds: None)

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, -2147024894, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)

    assert windows_service._query_task_xml(optional=True) is None
    assert len(calls) == windows_service.TASK_QUERY_ATTEMPTS
    with pytest.raises(windows_service.WindowsServiceError, match="not registered"):
        windows_service._query_task_xml(optional=False)


def test_query_task_xml_does_not_treat_other_query_failure_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        windows_service,
        "_run_schtasks",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            -2147024891,
            stdout=b"",
            stderr=b"",
        ),
    )

    with pytest.raises(
        windows_service.WindowsServiceError,
        match="hresult=0x80070005",
    ):
        windows_service._query_task_xml(optional=True)


def test_status_exposes_task_query_failure_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(
        windows_service,
        "_query_task_xml",
        lambda **_kwargs: (_ for _ in ()).throw(
            windows_service.WindowsServiceError(
                "Windows Task Scheduler query failed (hresult=0x80070005)"
            )
        ),
    )
    monkeypatch.setattr(windows_service, "_read_state", lambda: {})

    snapshot = windows_service.status()

    assert snapshot["registered"] is None
    assert snapshot["enabled"] is None
    assert snapshot["task_valid"] is False
    assert snapshot["task_query_error"].endswith("0x80070005)")
    assert snapshot["task_error"] == snapshot["task_query_error"]


def test_install_registers_only_the_task_xml_without_starting(
    tmp_path: Path,
    service_files: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, env, runner, key = service_files
    calls: list[tuple[str, ...]] = []
    home = tmp_path / "service-home"

    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(windows_service, "_runner_executable", lambda: runner)
    monkeypatch.setattr(windows_service, "default_api_key_path", lambda: key)
    monkeypatch.setattr(windows_service, "_find_legacy_tunnel_roots", lambda: [])

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(
        windows_service,
        "status",
        lambda: {"registered": True, "enabled": False, "task_valid": True},
    )

    snapshot = windows_service.install(config, env)

    assert snapshot["task_valid"] is True
    assert len(calls) == 1
    assert calls[0][:3] == ("/Create", "/TN", windows_service.TASK_NAME)
    assert "/XML" in calls[0]
    assert "/F" in calls[0]
    assert "/Run" not in calls[0]
    manifest = json.loads(windows_service.manifest_path().read_text(encoding="utf-8"))
    assert manifest["backend"] == windows_service.BACKEND
    assert manifest["user_sid"] == "S-1-5-21-1-2-3-1001"
    task_xml = windows_service.task_xml_path().read_text(encoding="utf-16")
    assert "secret-sentinel" not in task_xml
    assert "<TimeTrigger>" in task_xml
    assert "<Enabled>false</Enabled>" in task_xml


def test_install_rolls_back_fresh_registration_when_contract_verification_fails(
    tmp_path: Path,
    service_files: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, env, runner, key = service_files
    calls: list[tuple[str, ...]] = []
    home = tmp_path / "service-home"

    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(windows_service, "_runner_executable", lambda: runner)
    monkeypatch.setattr(windows_service, "default_api_key_path", lambda: key)
    monkeypatch.setattr(windows_service, "_find_legacy_tunnel_roots", lambda: [])
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(
        windows_service,
        "status",
        lambda: {"registered": True, "enabled": True, "task_valid": False},
    )

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)

    with pytest.raises(windows_service.WindowsServiceError, match="contract verification"):
        windows_service.install(config, env)

    assert calls[0][:3] == ("/Create", "/TN", windows_service.TASK_NAME)
    assert calls[-1] == ("/Delete", "/TN", windows_service.TASK_NAME, "/F")
    assert not windows_service.manifest_path().exists()
    assert not windows_service.task_xml_path().exists()


def test_install_restores_previous_files_and_registration_on_failed_update(
    tmp_path: Path,
    service_files: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, env, runner, key = service_files
    home = tmp_path / "service-home"
    home.mkdir()
    previous_manifest = b'{"previous":true}'
    previous_task_file = b"previous-task-file"
    (home / "service.json").write_bytes(previous_manifest)
    (home / "task.xml").write_bytes(previous_task_file)
    previous_registered_xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" />'
    )
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(windows_service, "_runner_executable", lambda: runner)
    monkeypatch.setattr(windows_service, "default_api_key_path", lambda: key)
    monkeypatch.setattr(windows_service, "_find_legacy_tunnel_roots", lambda: [])
    monkeypatch.setattr(
        windows_service,
        "_query_task_xml",
        lambda **_kwargs: previous_registered_xml,
    )
    monkeypatch.setattr(windows_service, "status", lambda: {"task_valid": False})

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)

    with pytest.raises(windows_service.WindowsServiceError, match="contract verification"):
        windows_service.install(config, env)

    create_calls = [call for call in calls if call[0] == "/Create"]
    assert len(create_calls) == 2
    assert ".rollback-task.xml" in create_calls[-1][4]
    assert windows_service.manifest_path().read_bytes() == previous_manifest
    assert windows_service.task_xml_path().read_bytes() == previous_task_file
    assert not (home / ".rollback-task.xml").exists()


def test_find_legacy_tunnel_roots_accepts_nested_tunnel_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serverfs = Path(
        "C:/Users/lx/AppData/Local/ServerFS/connector-env/Scripts/serverfs.exe"
    ).resolve()
    tunnel = Path("C:/Users/lx/AppData/Local/ServerFS/bin/tunnel-client.exe").resolve()
    entries = (
        ProcessEntry(10, 1, "serverfs.exe"),
        ProcessEntry(11, 10, "python.exe"),
        ProcessEntry(12, 11, "python.exe"),
        ProcessEntry(13, 12, "tunnel-client.exe"),
        # A managed Task Scheduler tree may contain another tunnel-client,
        # but without the exact serverfs.exe root it must never be selected.
        ProcessEntry(20, 2, "pythonw.exe"),
        ProcessEntry(21, 20, "tunnel-client.exe"),
    )
    identities = {
        10: ProcessIdentity(10, serverfs, 100),
        11: ProcessIdentity(11, Path("C:/Python/python.exe"), 101),
        12: ProcessIdentity(12, Path("C:/Python/python.exe"), 102),
        13: ProcessIdentity(13, tunnel, 103),
        20: ProcessIdentity(20, Path("C:/Python/pythonw.exe"), 104),
        21: ProcessIdentity(21, tunnel, 105),
    }

    monkeypatch.setattr(windows_service.sys, "platform", "win32")
    monkeypatch.setattr(windows_service, "_legacy_serverfs_executable", lambda: serverfs)
    monkeypatch.setattr(windows_service, "_legacy_tunnel_client_executable", lambda: tunnel)
    monkeypatch.setattr(windows_process, "process_entries", lambda: entries)
    monkeypatch.setattr(windows_process, "process_identity", identities.get)

    roots = windows_service._find_legacy_tunnel_roots()

    assert len(roots) == 1
    assert roots[0]["root"]["pid"] == 10  # type: ignore[index]
    assert roots[0]["tunnel_client"]["pid"] == 13  # type: ignore[index]


def test_install_records_detected_legacy_handover(
    tmp_path: Path,
    service_files: tuple[Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, env, runner, key = service_files
    home = tmp_path / "service-home"
    root = {
        "root": {"pid": 41, "image_path": "C:/serverfs.exe", "creation_time": 100},
        "tunnel_client": {
            "pid": 42,
            "image_path": "C:/tunnel-client.exe",
            "creation_time": 101,
        },
    }
    written: list[list[dict[str, object]]] = []

    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(windows_service, "_runner_executable", lambda: runner)
    monkeypatch.setattr(windows_service, "default_api_key_path", lambda: key)
    monkeypatch.setattr(windows_service, "_find_legacy_tunnel_roots", lambda: [root])
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(windows_service, "_write_handover", lambda roots: written.append(roots))
    monkeypatch.setattr(windows_service, "status", lambda: {"task_valid": True})
    monkeypatch.setattr(
        windows_service,
        "_run_schtasks",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b""),
    )

    windows_service.install(config, env)

    assert written == [[root]]


def test_pending_handover_waits_for_start_cli_then_terminates_frozen_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initiator = ProcessIdentity(10, Path("C:/python.exe"), 100)
    root = ProcessIdentity(20, Path("C:/serverfs.exe"), 200)
    path = tmp_path / "handover.json"
    path.write_text("{}", encoding="utf-8")
    waits: list[ProcessIdentity] = []
    terminated: list[ProcessIdentity] = []
    events: list[str] = []
    document = {
        "schema_version": windows_service.HANDOVER_SCHEMA_VERSION,
        "roots": [{"root": windows_service._identity_document(root)}],
        "initiator": windows_service._identity_document(initiator),
    }

    monkeypatch.setattr(windows_service, "_read_handover", lambda: document)
    monkeypatch.setattr(windows_service, "handover_path", lambda: path)
    monkeypatch.setattr(
        windows_process,
        "same_process",
        lambda identity: identity == initiator,
    )
    monkeypatch.setattr(
        windows_process,
        "wait_process_gone",
        lambda identity, _timeout=15.0: not waits.append(identity),
    )
    monkeypatch.setattr(
        windows_service,
        "_terminate_legacy_root",
        lambda identity: terminated.append(identity),
    )
    monkeypatch.setattr(
        windows_service,
        "_append_lifecycle_event",
        lambda event, **_fields: events.append(event),
    )

    windows_service._perform_pending_handover()

    assert waits == [initiator]
    assert terminated == [root]
    assert events == ["legacy_handover_started", "legacy_handover_completed"]
    assert not path.exists()


def test_runner_refuses_unmanaged_legacy_tunnel_without_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_service, "_read_handover", lambda: None)
    monkeypatch.setattr(windows_service, "_find_legacy_tunnel_roots", lambda: [{"root": {}}])

    with pytest.raises(windows_service.WindowsServiceError, match="reinstall the service"):
        windows_service._perform_pending_handover()


def test_start_enables_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    manifest = {
        "user_sid": "sid",
        "runner_executable": "C:/pythonw.exe",
        "runner_arguments": [],
        "config_path": "C:/serverfs.toml",
    }
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_manifest_document", lambda: manifest)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: "<Task />")
    monkeypatch.setattr(windows_service, "validate_registered_task", lambda *_args: (True, ""))

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)
    monkeypatch.setattr(windows_service, "status", lambda: {"runner_alive": True})
    monkeypatch.setattr(
        windows_service,
        "_set_handover_initiator",
        lambda: calls.append(("INITIATOR",)),
    )

    assert windows_service.start() == {"runner_alive": True}
    assert calls == [
        ("/Change", "/TN", windows_service.TASK_NAME, "/ENABLE"),
        ("INITIATOR",),
        ("/Run", "/TN", windows_service.TASK_NAME),
    ]


def test_runner_identity_state_records_actual_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(123, Path("C:/uv/pythonw.exe"), 456)
    monkeypatch.setattr(windows_service.os, "getpid", lambda: 123)
    monkeypatch.setattr(
        windows_process,
        "process_identity",
        lambda pid: identity if pid == 123 else None,
    )

    assert windows_service._runner_identity_state() == {
        "runner_pid": 123,
        "runner_image_path": str(identity.image_path),
        "runner_creation_time": 456,
    }


def test_runner_alive_uses_recorded_identity_not_task_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ProcessIdentity(123, Path("C:/uv/pythonw.exe"), 456)
    seen: list[ProcessIdentity] = []

    def fake_same_process(identity: ProcessIdentity) -> bool:
        seen.append(identity)
        return True

    monkeypatch.setattr(windows_process, "same_process", fake_same_process)
    monkeypatch.setattr(
        windows_service,
        "_process_image_path",
        lambda _pid: pytest.fail("launcher-path fallback must not be used"),
    )

    assert windows_service._runner_alive(
        {
            "runner_pid": 123,
            "runner_image_path": "C:/uv/pythonw.exe",
            "runner_creation_time": 456,
        },
        {"runner_executable": "C:/workspace/.venv/Scripts/pythonw.exe"},
    )
    assert seen == [expected]


def test_stop_disables_before_ending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: "<Task />")
    monkeypatch.setattr(windows_service, "_read_state", lambda: {})
    monkeypatch.setattr(windows_service, "_wait_runner_stopped", lambda *_args: None)

    def fake_schtasks(*args: str, check: bool = True):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service, "_run_schtasks", fake_schtasks)
    monkeypatch.setattr(windows_service, "status", lambda: {"runner_alive": False})

    assert windows_service.stop() == {"runner_alive": False}
    assert calls[0] == ("/Change", "/TN", windows_service.TASK_NAME, "/DISABLE")
    assert calls[1] == ("/End", "/TN", windows_service.TASK_NAME)


def test_stop_marks_operator_requested_stop_for_a_live_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: "<Task />")
    monkeypatch.setattr(
        windows_service,
        "_read_state",
        lambda: {"runner_pid": 123, "generation": 2},
    )
    monkeypatch.setattr(windows_service, "_manifest_document", lambda: {"runner_executable": "x"})
    monkeypatch.setattr(windows_service, "_runner_alive", lambda *_args: True)
    monkeypatch.setattr(windows_service, "_write_state", lambda document: writes.append(document))
    monkeypatch.setattr(windows_service, "_wait_runner_stopped", lambda *_args: None)
    monkeypatch.setattr(
        windows_service,
        "_run_schtasks",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(windows_service, "status", lambda: {"runner_alive": False})

    windows_service.stop()

    assert len(writes) == 1
    assert writes[0]["runner_pid"] == 123
    assert isinstance(writes[0]["stop_requested_at"], float)


def test_stop_cleans_verified_orphan_when_task_registration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(windows_service, "_read_state", lambda: {})
    monkeypatch.setattr(
        windows_service,
        "_terminate_managed_tree_if_alive",
        lambda: calls.append("terminate") or True,
    )
    monkeypatch.setattr(
        windows_service,
        "_wait_runner_stopped",
        lambda *_args: calls.append("wait"),
    )
    monkeypatch.setattr(windows_service, "status", lambda: {"runner_alive": False})

    assert windows_service.stop() == {"runner_alive": False}
    assert calls == ["terminate", "wait"]


def test_orphan_cleanup_uses_frozen_watchdog_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = ProcessIdentity(321, Path("C:/Python/pythonw.exe"), 987654)
    runner = ProcessIdentity(322, Path("C:/Python/pythonw.exe"), 987655)
    commands: list[list[str]] = []
    waited: list[ProcessIdentity] = []
    monkeypatch.setattr(
        windows_service,
        "_read_watchdog_state",
        lambda: {
            "watchdog_pid": watchdog.pid,
            "watchdog_image_path": str(watchdog.image_path),
            "watchdog_creation_time": watchdog.creation_time,
        },
    )
    monkeypatch.setattr(
        windows_service,
        "_read_state",
        lambda: {
            "runner_pid": runner.pid,
            "runner_image_path": str(runner.image_path),
            "runner_creation_time": runner.creation_time,
        },
    )
    monkeypatch.setattr(
        windows_process,
        "same_process",
        lambda identity: identity == watchdog,
    )
    monkeypatch.setattr(
        windows_process,
        "wait_process_gone",
        lambda identity, _timeout=15.0: not waited.append(identity),
    )
    monkeypatch.setattr(windows_service, "_taskkill_executable", lambda: Path("taskkill.exe"))

    def fake_run(args, **_kwargs):
        commands.append([str(value) for value in args])
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(windows_service.subprocess, "run", fake_run)

    assert windows_service._terminate_managed_tree_if_alive() is True
    assert commands == [["taskkill.exe", "/PID", "321", "/T", "/F"]]
    assert waited == [watchdog]


def test_uninstall_stops_orphan_even_when_task_is_already_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "service-home"
    home.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(windows_service, "stop", lambda: calls.append("stop") or {})

    windows_service.uninstall()

    assert calls == ["stop"]
    assert not home.exists()


def test_status_flags_mid_generation_runner_disappearance_as_unclean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(
        windows_service,
        "_read_state",
        lambda: {
            "runner_pid": 123,
            "runner_started_at": 10.0,
            "generation": 2,
            "last_tunnel_started_at": 13.0,
            "last_tunnel_exit_at": 12.0,
        },
    )
    monkeypatch.setattr(windows_service, "_read_watchdog_state", lambda: {})

    snapshot = windows_service.status()

    assert snapshot["runner_alive"] is False
    assert snapshot["runner_unclean_exit"] is True


def test_status_does_not_flag_an_intentional_stop_as_unclean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: None)
    monkeypatch.setattr(
        windows_service,
        "_read_state",
        lambda: {
            "runner_pid": 123,
            "generation": 1,
            "last_tunnel_started_at": 11.0,
            "last_tunnel_exit_at": None,
            "stop_requested_at": 12.0,
        },
    )
    monkeypatch.setattr(windows_service, "_read_watchdog_state", lambda: {})

    assert windows_service.status()["runner_unclean_exit"] is False


def test_uninstall_reports_task_removed_when_artifact_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "service-home"
    home.mkdir()
    monkeypatch.setattr(windows_service, "_ensure_windows", lambda: None)
    monkeypatch.setattr(windows_service, "service_home", lambda: home)
    monkeypatch.setattr(windows_service, "_query_task_xml", lambda **_kwargs: "<Task />")
    monkeypatch.setattr(windows_service, "stop", lambda: {})
    monkeypatch.setattr(
        windows_service,
        "_run_schtasks",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b""),
    )

    def fail_rmtree(_path: Path) -> None:
        raise OSError("sentinel")

    monkeypatch.setattr(windows_service.shutil, "rmtree", fail_rmtree)

    with pytest.raises(
        windows_service.WindowsServiceError,
        match="persistent task was removed, but local service artifacts",
    ):
        windows_service.uninstall()


def test_restart_backoff_is_bounded_and_resets_after_healthy_run() -> None:
    delay = windows_service.INITIAL_RESTART_DELAY_SECONDS
    delay = windows_service._next_restart_delay(1.0, delay)
    assert delay == 10.0
    delay = windows_service._next_restart_delay(1.0, delay)
    assert delay == 20.0
    delay = windows_service._next_restart_delay(1.0, delay)
    assert delay == 40.0
    delay = windows_service._next_restart_delay(1.0, delay)
    assert delay == windows_service.MAX_RESTART_DELAY_SECONDS
    assert (
        windows_service._next_restart_delay(windows_service.HEALTHY_RUN_RESET_SECONDS, delay)
        == windows_service.INITIAL_RESTART_DELAY_SECONDS
    )


def test_watchdog_restart_backoff_is_fast_bounded_and_resets() -> None:
    delay = windows_service.WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    delay = windows_service._next_watchdog_restart_delay(1.0, delay)
    assert delay == 4.0
    delay = windows_service._next_watchdog_restart_delay(1.0, delay)
    assert delay == 8.0
    delay = windows_service._next_watchdog_restart_delay(1.0, delay)
    assert delay == 16.0
    delay = windows_service._next_watchdog_restart_delay(1.0, delay)
    assert delay == windows_service.WATCHDOG_MAX_RESTART_DELAY_SECONDS
    assert (
        windows_service._next_watchdog_restart_delay(
            windows_service.WATCHDOG_HEALTHY_RESET_SECONDS, delay
        )
        == windows_service.WATCHDOG_INITIAL_RESTART_DELAY_SECONDS
    )


def test_runtime_env_file_is_authoritative_for_launcher_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVERFS_AGENT_PROXY_URL", "ambient")
    monkeypatch.setenv("SERVERFS_AGENT_NO_PROXY", "ambient")
    monkeypatch.setenv("SERVERFS_BRIDGE_PYTHON", "ambient")
    monkeypatch.setenv("SERVERFS_DATA_HOME", "ambient")

    windows_service._apply_runtime_environment(
        {
            "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:7897",
            "SERVERFS_BRIDGE_PYTHON": "C:/bridge/python.exe",
        }
    )

    assert windows_service.os.environ["SERVERFS_AGENT_PROXY_URL"] == "http://127.0.0.1:7897"
    assert windows_service.os.environ["SERVERFS_BRIDGE_PYTHON"] == "C:/bridge/python.exe"
    assert "SERVERFS_AGENT_NO_PROXY" not in windows_service.os.environ
    assert "SERVERFS_DATA_HOME" not in windows_service.os.environ
