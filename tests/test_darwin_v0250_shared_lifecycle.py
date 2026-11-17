"""Synthetic macOS Shared Core lifecycle: no Bridge ownership and scoped rollback."""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import darwin_instance_cli
from serverfs_mcp import darwin_instance_shared_lifecycle as shared
from serverfs_mcp.instance_identity import InstanceIdentity

IDENTITY = InstanceIdentity("core-a", "shared", "owner-bridge")


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    status: str = "not-registered",
    real_lock: bool = False,
):
    events: list[tuple] = []
    unregistered = False
    socket = tmp_path / "bridge.sock"
    config = tmp_path / "serverfs.toml"
    # Most lifecycle cases exercise manager behavior; focused tests below use
    # a real staged services.json inode to verify the cross-process lock.
    if not real_lock:
        monkeypatch.setattr(shared, "_shared_start_lock", lambda *_a: nullcontext())

    def resolve(identity, supplied_config, port, *, home=None, expected_executable=None):
        events.append(("manifest", identity, supplied_config, port, home))
        assert expected_executable == tmp_path / "core-python"
        return "Repo", socket

    async def prepare(identity, supplied_config, supplied_socket, *, home=None):
        events.append(("bridge-auth", identity, supplied_config, supplied_socket, home))
        return SimpleNamespace(_workdir_alias="Repo")

    def manager(identity, operation, component, *, home=None):
        nonlocal unregistered
        events.append(("manager", operation, component))
        assert identity == IDENTITY
        assert component == "http-origin"  # Never register/unregister the shared Bridge.
        if operation == "status":
            return "not-registered" if unregistered else status
        if operation == "unregister":
            unregistered = True
        return {
            "register": "enabled",
            "unregister": "not-registered",
        }[operation]

    monkeypatch.setattr(
        shared, "_signed_shared_core_python", lambda _id, _home: tmp_path / "core-python"
    )
    monkeypatch.setattr(shared, "resolve_shared_core_manifest", resolve)
    monkeypatch.setattr(shared, "prepare_shared_v2_client", prepare)
    monkeypatch.setattr(shared, "_manager", manager)
    monkeypatch.setattr(
        shared.darwin_service,
        "_launchd_job_pid",
        lambda label: events.append(("pid", label)) or None,
    )
    monkeypatch.setattr(
        shared.darwin_service,
        "_launchd_job_present",
        lambda label: events.append(("present", label)) or False,
    )
    monkeypatch.setattr(
        shared,
        "_assert_available_port",
        lambda *_a, **_k: events.append(("port-check",)),
    )
    monkeypatch.setattr(
        shared,
        "_wait_component_stopped",
        lambda identity, component, **_k: events.append(("wait-stop", component)),
    )
    from serverfs_mcp import darwin_instance_owned_lifecycle

    monkeypatch.setattr(
        darwin_instance_owned_lifecycle,
        "_core_ready",
        lambda identity, port: events.append(("core-ready", port)) or 4321,
    )
    return config, events


def _manifest_lock_file(home: Path) -> Path:
    path = shared.instance_service_home(IDENTITY, home=home) / "services.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_shared_start_lock_contention_fails_before_preflight_and_register(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fcntl = pytest.importorskip("fcntl")
    config, events = _setup(monkeypatch, tmp_path, real_lock=True)
    path = _manifest_lock_file(tmp_path)
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(shared.DarwinSharedLifecycleError, match="start lock"):
            shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
        assert events == []
    finally:
        os.close(fd)


def test_shared_start_lock_release_allows_retry_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("fcntl")
    config, events = _setup(monkeypatch, tmp_path, real_lock=True)
    _manifest_lock_file(tmp_path)
    monkeypatch.setattr(
        shared, "preflight_shared_core", lambda *_a, **_k: (_ for _ in ()).throw(ValueError())
    )
    with pytest.raises(ValueError):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    monkeypatch.setattr(shared, "preflight_shared_core", lambda *_a, **_k: {})
    monkeypatch.setattr(shared, "_manager", lambda *_a, **_k: "enabled")
    from serverfs_mcp import darwin_instance_owned_lifecycle

    monkeypatch.setattr(darwin_instance_owned_lifecycle, "_core_ready", lambda *_a: 4321)
    shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert events == []


def test_shared_start_lock_rejects_symlink_and_insecure_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("fcntl")
    config, events = _setup(monkeypatch, tmp_path, real_lock=True)
    path = _manifest_lock_file(tmp_path)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="start lock"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert events == []
    path.unlink()
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="start lock"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert events == []


def test_shared_start_lock_rejects_wrong_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("fcntl")
    config, events = _setup(monkeypatch, tmp_path, real_lock=True)
    _manifest_lock_file(tmp_path)
    original_fstat = os.fstat

    def wrong_owner(fd: int):
        result = original_fstat(fd)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_uid=result.st_uid + 1,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fstat", wrong_owner)
        with pytest.raises(shared.DarwinSharedLifecycleError, match="start lock"):
            shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert events == []


def _managers(events: list[tuple]) -> list[tuple]:
    return [event for event in events if event[0] == "manager"]


def test_shared_preflight_authenticates_real_owner_contract_without_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    verified = shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert verified == {
        "instance_id": "core-a",
        "bridge_mode": "shared",
        "bridge_id": "owner-bridge",
        "core_port": 18271,
        "core_registration": "not-registered",
        "bridge_authenticated": True,
        "state": "ready-to-register",
    }
    steps = [event[0] for event in events]
    assert steps.index("manifest") < steps.index("bridge-auth") < steps.index("manager")
    assert "port-check" in steps
    assert _managers(events) == [("manager", "status", "http-origin")]
    assert not any(event[0] in {"wait-stop", "core-ready"} for event in events)


@pytest.mark.parametrize("presence", [True, None, 0, ""])
def test_shared_preflight_rejects_loaded_or_malformed_presence_without_register(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, presence: object
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", lambda _label: presence)
    with pytest.raises(shared.DarwinSharedLifecycleError):
        shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "register" not in [event[1] for event in _managers(events)]


def test_shared_preflight_sanitizes_presence_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shared.darwin_service,
        "_launchd_job_present",
        lambda _label: (_ for _ in ()).throw(RuntimeError("private launchctl text")),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError) as error:
        shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "private launchctl text" not in str(error.value)
    assert "register" not in [event[1] for event in _managers(events)]


@pytest.mark.parametrize("broken", ["manifest", "owner-unavailable", "alias", "port", "registered"])
def test_shared_preflight_fails_closed_and_never_registers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, broken: str
) -> None:
    config, events = _setup(
        monkeypatch, tmp_path, status=("enabled" if broken == "registered" else "not-registered")
    )
    if broken == "manifest":
        monkeypatch.setattr(
            shared,
            "resolve_shared_core_manifest",
            lambda *_a, **_k: (_ for _ in ()).throw(
                shared.DarwinSharedManifestError("synthetic private secret")
            ),
        )
    elif broken == "owner-unavailable":

        async def offline(*_a, **_k):
            raise shared.SharedCoreBootstrapError("synthetic private secret")

        monkeypatch.setattr(shared, "prepare_shared_v2_client", offline)
    elif broken == "alias":

        async def wrong(*_a, **_k):
            return SimpleNamespace(_workdir_alias="foreign")

        monkeypatch.setattr(shared, "prepare_shared_v2_client", wrong)
    elif broken == "port":
        monkeypatch.setattr(
            shared,
            "_assert_available_port",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("port unavailable")),
        )
    with pytest.raises((shared.DarwinSharedLifecycleError, ValueError)) as error:
        shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "synthetic private secret" not in str(error.value)
    assert "register" not in [event[1] for event in _managers(events)]
    assert "unregister" not in [event[1] for event in _managers(events)]


@pytest.mark.parametrize("state", [[], {}, "unknown-state"])
def test_shared_preflight_rejects_malformed_or_unknown_manager_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: object
) -> None:
    config, events = _setup(monkeypatch, tmp_path)

    def manager(_identity, operation, component, *, home=None):
        events.append(("manager", operation, component))
        assert operation == "status" and component == "http-origin"
        return state

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError) as error:
        shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "already registered" not in str(error.value)
    assert _managers(events) == [("manager", "status", "http-origin")]
    assert not any(event[0] in {"wait-stop", "core-ready"} for event in events)


def test_shared_preflight_sanitizes_manager_status_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)

    def manager(_identity, operation, component, *, home=None):
        events.append(("manager", operation, component))
        raise RuntimeError("private path and synthetic secret")

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="state is unavailable") as error:
        shared.preflight_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "private path" not in str(error.value)
    assert "synthetic secret" not in str(error.value)
    assert _managers(events) == [("manager", "status", "http-origin")]


def test_shared_start_uses_exact_same_read_only_preflight_before_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    original = shared.preflight_shared_core
    seen: list[tuple] = []

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.append((args, kwargs, result))
        return result

    monkeypatch.setattr(shared, "preflight_shared_core", wrapped)
    shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert len(seen) == 1
    assert seen[0][0] == (IDENTITY, config, 18271)
    assert seen[0][1] == {"home": tmp_path}
    assert seen[0][2]["bridge_authenticated"] is True
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
    ]


def test_shared_start_preserves_invalid_identity_error_before_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shared,
        "_shared_start_lock",
        lambda *_a: pytest.fail("invalid identity must be rejected before lock access"),
    )
    invalid = InstanceIdentity("default", "shared", "owner-bridge")
    with pytest.raises(shared.DarwinSharedLifecycleError, match="identity required"):
        shared.start_shared_core(invalid, config, 18271, home=tmp_path)
    assert events == []


def test_shared_start_registers_only_core_after_authenticated_live_v2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    result = shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert result == {
        "instance_id": "core-a",
        "bridge_mode": "shared",
        "bridge_id": "owner-bridge",
        "core_registration": "enabled",
        "core_pid": 4321,
        "core_port": 18271,
    }
    steps = [entry[0] for entry in events]
    assert steps.index("manifest") < steps.index("bridge-auth") < steps.index("manager")
    assert steps.index("port-check") < steps.index("core-ready")
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
    ]
    assert ("wait-stop", "core") not in events


@pytest.mark.parametrize(
    "status",
    ["enabled", "requires-approval"],
)
def test_shared_start_refuses_existing_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    config, events = _setup(monkeypatch, tmp_path, status=status)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="already registered"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [("manager", "status", "http-origin")]


@pytest.mark.parametrize("failed", ["python-pin", "manifest", "bridge-auth", "alias"])
def test_shared_start_fails_closed_before_register(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed: str
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    if failed == "python-pin":
        monkeypatch.setattr(
            shared,
            "_signed_shared_core_python",
            lambda *_a: (_ for _ in ()).throw(
                shared.DarwinSharedLifecycleError("synthetic private secret")
            ),
        )
    elif failed == "manifest":
        monkeypatch.setattr(
            shared,
            "resolve_shared_core_manifest",
            lambda *_a, **_k: (_ for _ in ()).throw(
                shared.DarwinSharedManifestError("synthetic private secret")
            ),
        )
    elif failed == "bridge-auth":

        async def fail(*_a, **_k):
            raise shared.SharedCoreBootstrapError("synthetic private secret")

        monkeypatch.setattr(shared, "prepare_shared_v2_client", fail)
    else:

        async def wrong_alias(*_a, **_k):
            return SimpleNamespace(_workdir_alias="other")

        monkeypatch.setattr(shared, "prepare_shared_v2_client", wrong_alias)
    with pytest.raises(shared.DarwinSharedLifecycleError) as exc:
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert "synthetic private secret" not in str(exc.value)
    assert _managers(events) == []


def test_shared_start_rolls_back_only_new_core_on_failed_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    from serverfs_mcp import darwin_instance_owned_lifecycle

    def not_ready(identity, port):
        events.append(("core-not-ready", port))
        raise ValueError("simulated failed native Core")

    monkeypatch.setattr(darwin_instance_owned_lifecycle, "_core_ready", not_ready)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rolled back"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
        ("manager", "unregister", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert ("wait-stop", "core") in events
    assert events.index(("wait-stop", "core")) < events.index(
        ("manager", "status", "http-origin"), 3
    )
    assert ("pid", IDENTITY.macos_label("core")) in events


def test_shared_start_rollback_waits_for_delayed_core_exit_before_final_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    pid_state: int | None = None
    from serverfs_mcp import darwin_instance_owned_lifecycle

    def core_ready(_identity, _port):
        nonlocal pid_state
        pid_state = 4321
        raise RuntimeError("simulated failed readiness")

    def pid(label):
        events.append(("pid", label))
        assert label == IDENTITY.macos_label("core")
        return pid_state

    def wait(_identity, component, **kwargs):
        nonlocal pid_state
        events.append(("wait-stop", component))
        assert component == "core" and kwargs["timeout"] == 12.0
        assert pid_state == 4321
        pid_state = None

    monkeypatch.setattr(darwin_instance_owned_lifecycle, "_core_ready", core_ready)
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_pid", pid)
    monkeypatch.setattr(shared, "_wait_component_stopped", wait)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rolled back"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    wait_index = events.index(("wait-stop", "core"))
    assert events[wait_index + 1] == ("manager", "status", "http-origin")
    assert events[wait_index + 2] == ("pid", IDENTITY.macos_label("core"))


@pytest.mark.parametrize(
    "failure", ["reregistered", "malformed-status", "status-error", "live-pid", "pid-error"]
)
def test_shared_start_rollback_fails_closed_on_unverified_terminal_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    from serverfs_mcp import darwin_instance_owned_lifecycle

    status_calls = 0

    def manager(_identity, operation, component, *, home=None):
        nonlocal status_calls
        events.append(("manager", operation, component))
        assert component == "http-origin"
        if operation == "status":
            status_calls += 1
            if status_calls == 1:
                return "not-registered"
            if failure == "reregistered":
                return "enabled"
            if failure == "malformed-status":
                return []
            if failure == "status-error":
                raise RuntimeError("private manager detail")
            return "not-registered"
        if operation == "register":
            return "enabled"
        if operation == "unregister":
            return "not-registered"
        pytest.fail(f"unexpected manager operation: {operation}")

    pid_calls = 0

    def pid(label):
        nonlocal pid_calls
        pid_calls += 1
        events.append(("pid", label))
        assert label == IDENTITY.macos_label("core")
        if pid_calls == 1:
            return None
        if failure == "live-pid":
            return 4321
        if failure == "pid-error":
            raise RuntimeError("private PID detail")
        return None

    monkeypatch.setattr(shared, "_manager", manager)
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_pid", pid)
    monkeypatch.setattr(
        darwin_instance_owned_lifecycle,
        "_core_ready",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rollback failed"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
        ("manager", "unregister", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert events.index(("wait-stop", "core")) < events.index(
        ("manager", "status", "http-origin"), 3
    )
    pid_checks = [event for event in events if event[0] == "pid"]
    expected_pid_checks = 1 if failure == "status-error" else 2
    assert pid_checks == [("pid", IDENTITY.macos_label("core"))] * expected_pid_checks
    assert sum(event[0] == "manifest" for event in events) == 1
    assert sum(event[0] == "bridge-auth" for event in events) == 1
    assert all(event[2] == "http-origin" for event in _managers(events))
    assert not any(
        event[0] == "manager"
        and event[1] in {"register", "unregister"}
        and event[2] == "agent-bridge"
        for event in events
    )


def test_shared_start_rollback_rejects_loaded_job_without_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    from serverfs_mcp import darwin_instance_owned_lifecycle

    monkeypatch.setattr(
        darwin_instance_owned_lifecycle,
        "_core_ready",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    presence_calls = 0

    def presence(_label):
        nonlocal presence_calls
        presence_calls += 1
        return presence_calls > 1

    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", presence)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rollback failed"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert [event[1] for event in _managers(events) if event[0] == "manager"].count(
        "unregister"
    ) == 1
    assert [event[1] for event in _managers(events) if event[0] == "manager"].count("register") == 1


@pytest.mark.parametrize("presence", [0, None])
def test_shared_start_rollback_rejects_non_false_presence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, presence: object
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    from serverfs_mcp import darwin_instance_owned_lifecycle

    calls = 0

    def present(_label):
        nonlocal calls
        calls += 1
        return False if calls == 1 else presence

    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", present)
    monkeypatch.setattr(
        darwin_instance_owned_lifecycle,
        "_core_ready",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rollback failed"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert calls == 2


def test_shared_stop_rejects_loaded_job_without_pid_after_unregister(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", lambda _label: True)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="stop state is indeterminate"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert [event[1] for event in _managers(events) if event[0] == "manager"].count(
        "unregister"
    ) == 1


@pytest.mark.parametrize("presence", [0, None])
def test_shared_stop_rejects_non_false_presence_after_unregister(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, presence: object
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", lambda _label: presence)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="stop state is indeterminate"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert [event[1] for event in _managers(events) if event[0] == "manager"].count(
        "unregister"
    ) == 1


def test_shared_stop_refuses_absent_manager_status_with_loaded_job_without_unregister(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(shared.darwin_service, "_launchd_job_present", lambda _label: True)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="inconsistent"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert "unregister" not in [event[1] for event in _managers(events)]


@pytest.mark.parametrize(
    ("registered", "pid"),
    [
        ("not-registered", None),
        ("enabled", 4321),
        ("unknown-state", None),
        ([], None),
        ({}, None),
    ],
)
def test_register_exception_preserves_state_without_unregistration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registered: object,
    pid: int | None,
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    calls = 0

    def manager(identity, operation, component, *, home=None):
        nonlocal calls
        events.append(("manager", operation, component))
        assert component == "http-origin"
        if operation == "register":
            calls += 1
            raise RuntimeError("register uncertain")
        if operation == "status":
            return registered if calls else "not-registered"
        return "not-registered"

    monkeypatch.setattr(shared, "_manager", manager)
    monkeypatch.setattr(
        shared.darwin_service,
        "_launchd_job_pid",
        lambda label: events.append(("pid", label)) or (pid if calls else None),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="no unregister"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert ("pid", IDENTITY.macos_label("core")) in events
    assert not any(event[1] == "unregister" for event in _managers(events))


@pytest.mark.parametrize("malformed", [[], {}])
def test_register_exception_malformed_status_is_unknown_without_unregistration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, malformed: object
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    attempted = False

    def manager(_identity, operation, component, *, home=None):
        nonlocal attempted
        events.append(("manager", operation, component))
        assert component == "http-origin"
        if operation == "register":
            attempted = True
            raise RuntimeError("register outcome uncertain")
        if operation == "status" and attempted:
            return malformed
        return "not-registered"

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="status=unknown"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "register", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert not any(event[1] == "unregister" for event in _managers(events))


@pytest.mark.parametrize("failure", ["status", "pid"])
def test_register_exception_inspection_failure_preserves_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    config, events = _setup(monkeypatch, tmp_path)

    attempted = False

    def manager(identity, operation, component, *, home=None):
        nonlocal attempted
        events.append(("manager", operation, component))
        if operation == "register":
            attempted = True
            raise RuntimeError("synthetic failure")
        if operation == "status" and attempted and failure == "status":
            raise RuntimeError("synthetic status failure")
        return "not-registered"

    monkeypatch.setattr(shared, "_manager", manager)
    if failure == "pid":
        calls = 0

        def pid_lookup(_label):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("pid unavailable")
            return None

        monkeypatch.setattr(
            shared.darwin_service,
            "_launchd_job_pid",
            pid_lookup,
        )
    with pytest.raises(shared.DarwinSharedLifecycleError):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert not any(event[1] == "unregister" for event in _managers(events))


def test_requires_approval_rolls_back_only_http_origin_and_checks_terminal_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    original = shared._manager

    def approval_manager(identity, operation, component, *, home=None):
        if operation == "register":
            events.append(("manager", operation, component))
            return "requires-approval"
        return original(identity, operation, component, home=home)

    monkeypatch.setattr(shared, "_manager", approval_manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="explicit macOS approval"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events)[-2:] == [
        ("manager", "unregister", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert ("wait-stop", "core") in events
    assert all(event[2] == "http-origin" for event in _managers(events))


@pytest.mark.parametrize("failure", ["unregister", "terminal", "wait"])
def test_failed_readiness_reports_scoped_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    config, events = _setup(monkeypatch, tmp_path)
    from serverfs_mcp import darwin_instance_owned_lifecycle

    monkeypatch.setattr(
        darwin_instance_owned_lifecycle,
        "_core_ready",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    original = shared._manager

    def manager(identity, operation, component, *, home=None):
        result = original(identity, operation, component, home=home)
        if operation == "unregister" and failure in {"unregister", "terminal"}:
            if failure == "unregister":
                raise RuntimeError("unregister failed")
            return "enabled"
        return result

    monkeypatch.setattr(shared, "_manager", manager)
    if failure == "wait":
        monkeypatch.setattr(
            shared,
            "_wait_component_stopped",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("still running")),
        )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="rollback failed"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)


def test_shared_start_rejects_occupied_tcp_port_before_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, events = _setup(monkeypatch, tmp_path)

    def occupied(*_a, **_k):
        raise ValueError("Core port occupied")

    monkeypatch.setattr(shared, "_assert_available_port", occupied)
    with pytest.raises(ValueError, match="port occupied"):
        shared.start_shared_core(IDENTITY, config, 18271, home=tmp_path)
    assert _managers(events) == [("manager", "status", "http-origin")]


def test_shared_stop_can_recover_corrupt_grant_without_touching_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    result = shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert result["core_registration"] == "not-registered"
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "unregister", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert ("wait-stop", "core") in events
    assert not any(item[0] in {"manifest", "bridge-auth"} for item in events)


def test_shared_stop_refuses_inactive_core(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, events = _setup(monkeypatch, tmp_path)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="already unregistered"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert _managers(events) == [("manager", "status", "http-origin")]


@pytest.mark.parametrize("state", ["unknown", "malformed", "", None, 17, [], {}])
def test_shared_stop_refuses_indeterminate_registration_without_unregister(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: object
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")

    def manager(_identity, operation, component, *, home=None):
        events.append(("manager", operation, component))
        assert component == "http-origin"
        assert operation == "status"
        return state

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="indeterminate"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert _managers(events) == [("manager", "status", "http-origin")]


@pytest.mark.parametrize("state", ["enabled", "requires-approval"])
def test_shared_stop_allows_only_known_registered_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status=state)
    result = shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert result["core_registration"] == "not-registered"
    assert _managers(events) == [
        ("manager", "status", "http-origin"),
        ("manager", "unregister", "http-origin"),
        ("manager", "status", "http-origin"),
    ]
    assert ("wait-stop", "core") in events
    assert ("pid", IDENTITY.macos_label("core")) in events


def test_shared_stop_registration_query_exception_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path)

    calls: list[tuple[str, str]] = []

    def fail(_identity, operation, component, *, home=None):
        calls.append((operation, component))
        assert component == "http-origin"
        assert operation == "status"
        raise RuntimeError("manager unavailable")

    monkeypatch.setattr(shared, "_manager", fail)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="state is unavailable"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert calls == [("status", "http-origin")]


def test_shared_stop_rejects_live_pid_when_registration_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shared.darwin_service,
        "_launchd_job_pid",
        lambda label: events.append(("pid", label)) or 4321,
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="inconsistent"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert _managers(events) == [("manager", "status", "http-origin")]
    assert ("pid", IDENTITY.macos_label("core")) in events


@pytest.mark.parametrize("unregister_result", ["enabled", "unknown"])
def test_shared_stop_requires_known_unregister_terminal_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unregister_result: str
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    original = shared._manager

    def manager(identity, operation, component, *, home=None):
        if operation == "unregister":
            events.append(("manager", operation, component))
            return unregister_result
        return original(identity, operation, component, home=home)

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="did not unregister"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert _managers(events)[-1] == ("manager", "unregister", "http-origin")


def test_shared_stop_manager_exception_never_unregisters_or_touches_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    calls: list[tuple[str, str]] = []

    def manager(_identity, operation, component, *, home=None):
        calls.append((operation, component))
        if operation == "status":
            return "enabled"
        raise RuntimeError("synthetic unregister failure")

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="unregistered safely"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert calls == [("status", "http-origin"), ("unregister", "http-origin")]


def test_shared_stop_waits_for_delayed_pid_drop_before_terminal_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    pid_state = {"stopped": False}

    def pid(label):
        events.append(("pid", label))
        assert label == IDENTITY.macos_label("core")
        return None if pid_state["stopped"] else 4321

    def wait(identity, component, **_kwargs):
        events.append(("wait-stop", component))
        pid_state["stopped"] = True

    monkeypatch.setattr(shared.darwin_service, "_launchd_job_pid", pid)
    monkeypatch.setattr(shared, "_wait_component_stopped", wait)
    result = shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert result["core_registration"] == "not-registered"
    assert events.index(("wait-stop", "core")) < max(
        index for index, event in enumerate(events) if event[0] == "pid"
    )


def test_shared_stop_rejects_terminal_status_drift_after_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    statuses = iter(["enabled", "enabled"])

    def manager(_identity, operation, component, *, home=None):
        events.append(("manager", operation, component))
        if operation == "status":
            return next(statuses)
        return "not-registered"

    monkeypatch.setattr(shared, "_manager", manager)
    with pytest.raises(shared.DarwinSharedLifecycleError, match="stop state is indeterminate"):
        shared.stop_shared_core(IDENTITY, home=tmp_path)
    assert events.index(("wait-stop", "core")) < max(
        index
        for index, event in enumerate(events)
        if event[0] == "manager" and event[1] == "status"
    )


def test_shared_status_is_read_only_and_scoped_to_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events = _setup(monkeypatch, tmp_path, status="enabled")
    status = shared.shared_core_status(IDENTITY, home=tmp_path)
    assert status["bridge_id"] == "owner-bridge"
    assert status["core_registration"] == "enabled"
    assert status["core_pid"] is None
    assert _managers(events) == [("manager", "status", "http-origin")]


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity(),
        InstanceIdentity("core-a", "disabled"),
        InstanceIdentity("core-a", "owned", "core-a"),
    ],
)
def test_shared_lifecycle_never_accepts_default_or_owned(
    monkeypatch: pytest.MonkeyPatch, identity: InstanceIdentity
) -> None:
    monkeypatch.setattr(
        shared, "_manager", lambda *_a, **_k: pytest.fail("no manager on wrong identity")
    )
    with pytest.raises(shared.DarwinSharedLifecycleError):
        shared.shared_core_status(identity)
    with pytest.raises(shared.DarwinSharedLifecycleError):
        shared.stop_shared_core(identity)
    with pytest.raises(shared.DarwinSharedLifecycleError):
        shared.start_shared_core(identity, Path("/synthetic/config"), 18271)


def test_native_start_derives_python_pin_from_verified_signed_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    app = tmp_path / "isolated" / "SignedServiceManager.app"
    pinned = tmp_path / "runtime" / "bin" / "serverfs-core-python"

    def signed_manager(identity, home):
        assert identity == IDENTITY and home == tmp_path
        events.append("codesign")
        return app / "Contents/MacOS/serverfs-service-manager"

    def app_for_id(identity, *, home=None):
        assert identity == IDENTITY and home == tmp_path
        events.append("app-identity")
        return app

    def info_for_app(path):
        assert path == app / "Contents/Info.plist"
        events.append("signed-plist")
        return {"ServerFSSharedCorePythonPath": str(pinned)}

    def check_python(path):
        events.append("python-identity")
        assert path == pinned
        return path

    monkeypatch.setattr(shared, "_manager_path", signed_manager)
    monkeypatch.setattr(shared, "instance_app_path", app_for_id)
    monkeypatch.setattr(shared, "_read_plist", info_for_app)
    monkeypatch.setattr(shared, "_validate_shared_core_python", check_python)
    monkeypatch.setattr(
        shared,
        "verify_managed_shared_core_python",
        lambda path: events.append("managed-provenance") or path == pinned,
    )
    assert shared._signed_shared_core_python(IDENTITY, tmp_path) == pinned
    assert events == [
        "codesign",
        "app-identity",
        "signed-plist",
        "python-identity",
        "managed-provenance",
    ]


@pytest.mark.parametrize("pin", [None, "", "relative/python", 456])
def test_shared_start_rejects_malformed_signed_python_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pin: object
) -> None:
    monkeypatch.setattr(shared, "_manager_path", lambda *_a: tmp_path / "manager")
    monkeypatch.setattr(shared, "instance_app_path", lambda *_a, **_k: tmp_path / "app")
    monkeypatch.setattr(shared, "_read_plist", lambda *_a: {"ServerFSSharedCorePythonPath": pin})
    monkeypatch.setattr(
        shared,
        "_validate_shared_core_python",
        lambda *_a: pytest.fail("never check a missing or invalid pin"),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="pin"):
        shared._signed_shared_core_python(IDENTITY, tmp_path)


def test_shared_preflight_rejects_managed_runtime_drift_before_manifest_or_native_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = tmp_path / "synthetic.app"
    python = tmp_path / "official" / "bin" / "serverfs-core-python"
    monkeypatch.setattr(shared, "_manager_path", lambda *_a: app / "manager")
    monkeypatch.setattr(shared, "instance_app_path", lambda *_a, **_k: app)
    monkeypatch.setattr(
        shared, "_read_plist", lambda *_a: {"ServerFSSharedCorePythonPath": str(python)}
    )
    monkeypatch.setattr(shared, "_validate_shared_core_python", lambda _a: python)

    def drift(_python):
        raise shared.DarwinSharedRuntimeError("synthetic private binary payload")

    monkeypatch.setattr(shared, "verify_managed_shared_core_python", drift)
    monkeypatch.setattr(
        shared,
        "resolve_shared_core_manifest",
        lambda *_a, **_k: pytest.fail("manifest must not run after failed provenance"),
    )
    monkeypatch.setattr(
        shared,
        "_manager",
        lambda *_a, **_k: pytest.fail("native status must not run after failed provenance"),
    )
    with pytest.raises(shared.DarwinSharedLifecycleError, match="preflight failed") as error:
        shared.preflight_shared_core(IDENTITY, tmp_path / "core.toml", 18271, home=tmp_path)
    assert "synthetic private binary payload" not in str(error.value)


def test_unsigned_app_is_rejected_before_reading_python_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject(*_a):
        raise ValueError("untrusted test-only app signature")

    monkeypatch.setattr(shared, "_manager_path", reject)
    monkeypatch.setattr(shared, "_read_plist", lambda *_a: pytest.fail("plist is untrusted"))
    with pytest.raises(ValueError, match="signature"):
        shared._signed_shared_core_python(IDENTITY, tmp_path)


def test_cli_explicit_shared_identity_never_falls_back_to_owned_or_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    captured: list[tuple] = []
    monkeypatch.setattr(
        shared,
        "shared_core_status",
        lambda identity: captured.append(("status", identity)) or {"ok": True},
    )
    monkeypatch.setattr(
        shared,
        "stop_shared_core",
        lambda identity: captured.append(("stop", identity)) or {"ok": True},
    )
    monkeypatch.setattr(
        shared,
        "start_shared_core",
        lambda identity, config, port: (
            captured.append(("start", identity, config, port)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        shared,
        "preflight_shared_core",
        lambda identity, config, port: (
            captured.append(("preflight", identity, config, port))
            or {
                "state": "ready-to-register",
                "bridge_authenticated": True,
            }
        ),
    )
    prefix = ["--instance", "core-a"]
    assert darwin_instance_cli.main([*prefix, "shared-status", "--bridge-id", "owner-bridge"]) == 0
    assert darwin_instance_cli.main([*prefix, "shared-stop", "--bridge-id", "owner-bridge"]) == 0
    assert (
        darwin_instance_cli.main(
            [
                *prefix,
                "shared-start",
                "--bridge-id",
                "owner-bridge",
                "--config",
                "/synthetic/core.toml",
                "--http-port",
                "18271",
            ]
        )
        == 0
    )
    assert (
        darwin_instance_cli.main(
            [
                *prefix,
                "shared-preflight",
                "--bridge-id",
                "owner-bridge",
                "--config",
                "/synthetic/core.toml",
                "--http-port",
                "18271",
            ]
        )
        == 0
    )
    assert [entry[0] for entry in captured] == ["status", "stop", "start", "preflight"]
    assert all(entry[1] == IDENTITY for entry in captured)
    assert captured[-1][2:] == (Path("/synthetic/core.toml"), 18271)
    assert capsys.readouterr().err == ""
