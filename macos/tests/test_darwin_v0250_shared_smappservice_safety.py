"""Safety guards for opt-in native Shared Core SMAppService acceptance.

Tests call no registration/unregister API. Unavailable or conflicting native
status must be UNKNOWN, not an invitation to stop or delete a live job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_darwin_v0250_shared_smappservice_live_macos as acceptance
from serverfs_mcp.instance_identity import InstanceIdentity

CORE = InstanceIdentity("shsm-synthetic", "shared", "synthetic-owner")


def test_protected_snapshot_includes_named_and_inactive_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_label = InstanceIdentity("shsm-a-0123456789", "shared", "shsm-owner").macos_label("core")
    rows = [
        "PID Status Label",
        "101 0 com.ntlx.serverfs.named.core",
        "- 1 com.ntlx.serverfs.named.cloudflare",
        "203 0 com.ntlx.serverfs.other.bridge",
        f"304 0 {test_label}",
        f"305 0 {test_label}.unexpected",
        "402 0 unrelated.service",
    ]
    monkeypatch.setattr(
        acceptance.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="\n".join(rows)),
    )
    expected = {
        "com.ntlx.serverfs.named.core": (101, 0),
        "com.ntlx.serverfs.named.cloudflare": (None, 1),
        "com.ntlx.serverfs.other.bridge": (203, 0),
        f"{test_label}.unexpected": (305, 0),
    }
    assert acceptance._protected_launchd_snapshot(frozenset({test_label})) == expected
    rows[1] = "102 0 com.ntlx.serverfs.named.core"
    assert acceptance._protected_launchd_snapshot(frozenset({test_label})) != expected
    rows[1] = "101 0 com.ntlx.serverfs.named.core"
    rows[2] = "- 2 com.ntlx.serverfs.named.cloudflare"
    assert acceptance._protected_launchd_snapshot(frozenset({test_label})) != expected
    rows.pop(2)
    assert acceptance._protected_launchd_snapshot(frozenset({test_label})) != expected


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, ""),
        (0, "invalid 0 com.ntlx.serverfs.core"),
        (0, "123 invalid com.ntlx.serverfs.core"),
        (0, "0 0 com.ntlx.serverfs.core"),
        (0, "123 com.ntlx.serverfs.core"),
        (0, "123 0 com.ntlx.serverfs.core\n124 0 com.ntlx.serverfs.core"),
    ],
)
def test_protected_snapshot_refuses_unverifiable_inventory(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        acceptance.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    with pytest.raises(RuntimeError):
        acceptance._protected_launchd_snapshot()


def test_authenticated_http_readiness_accepts_delayed_mcp_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = InstanceIdentity("shsm-b-0123456789", "shared", "shsm-owner")
    clock = [0.0]
    calls: list[tuple[int, str, str, dict[str, object]]] = []
    attempts = 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    async def mcp_call(port: int, token: str, method: str, params: dict[str, object]):
        nonlocal attempts
        attempts += 1
        calls.append((port, token, method, params))
        if attempts < 3:
            raise ConnectionError("synthetic endpoint startup delay")
        return SimpleNamespace(is_error=False, structured_content={"workdirs": []})

    monkeypatch.setattr(acceptance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(acceptance.time, "sleep", sleep)
    monkeypatch.setattr(acceptance.darwin_service, "_launchd_job_pid", lambda _label: 81234)
    monkeypatch.setattr(acceptance, "_mcp_call", mcp_call)
    asyncio.run(
        acceptance._wait_authenticated_core_http_ready(
            identity, 81234, 18313, "private-test-token", 2.0, lambda: True
        )
    )
    assert attempts == 3
    assert calls == [(18313, "private-test-token", "list_workdirs", {})] * 3
    assert clock == [1.0]


def test_authenticated_http_readiness_times_out_without_accepting_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = InstanceIdentity("shsm-b-0123456789", "shared", "shsm-owner")
    clock = [0.0]
    attempts = 0

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    async def unavailable(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("synthetic endpoint remains unavailable")

    monkeypatch.setattr(acceptance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(acceptance.time, "sleep", sleep)
    monkeypatch.setattr(acceptance.darwin_service, "_launchd_job_pid", lambda _label: 81234)
    monkeypatch.setattr(acceptance, "_mcp_call", unavailable)
    with pytest.raises(TimeoutError, match="did not become ready"):
        asyncio.run(
            acceptance._wait_authenticated_core_http_ready(
                identity, 81234, 18313, "private-test-token", 1.0, lambda: True
            )
        )
    assert attempts == 2
    assert clock == [1.0]


@pytest.mark.parametrize("tamper", ["valid", "uid", "port", "config", "argv", "label"])
def test_crash_pid_verification_refuses_mismatched_native_state_without_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    identity = InstanceIdentity("shsm-b-0123456789", "shared", "shsm-owner")
    label = identity.macos_label("core")
    pid = 81234
    python = Path("/opt/test/venv/bin/python")
    config = tmp_path / "core.toml"
    config.write_text("[server]\n", encoding="utf-8")
    other_config = tmp_path / "different.toml"
    other_config.write_text("[server]\n", encoding="utf-8")
    app = tmp_path / acceptance.APP_NAME
    agents = app / "Contents/Library/LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{label}.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "KeepAlive": True,
                "ProgramArguments": [
                    "serverfs-service-launcher",
                    identity.instance_id,
                    "http-origin",
                ],
            }
        )
    )
    (app / "Contents").mkdir(exist_ok=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.ntlx.serverfs.test"})
    )
    arguments = [
        "-B",
        "-m",
        "serverfs_mcp.cli",
        "serve",
        "--transport",
        "streamable-http",
        "--config",
        str(config),
        "--http-port",
        "18313",
        "--macos-instance",
        identity.instance_id,
        "--macos-shared-bridge",
        identity.bridge_id,
    ]
    manifest = {
        "owner": "v0.25-named-shared-core",
        "instance_id": identity.instance_id,
        "bridge_mode": "shared",
        "bridge_id": identity.bridge_id,
        "core": {"config": str(config), "port": 18313},
        "services": {"http-origin": {"executable": str(python), "arguments": arguments}},
    }
    manifest_path = tmp_path / "services.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(acceptance, "instance_service_home", lambda _identity: tmp_path)
    monkeypatch.setattr(acceptance, "_verified_test_manifest", lambda *_args: True)
    monkeypatch.setattr(acceptance, "_registration_state", lambda _identity: "registered")
    monkeypatch.setattr(acceptance.darwin_service, "_launchd_job_pid", lambda _label: pid)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(acceptance.os, "kill", lambda *args: signals.append(args))

    launch_label = "com.ntlx.serverfs.other.core" if tamper == "label" else label
    process_uid = os.getuid() + 1 if tamper == "uid" else os.getuid()
    command = [str(python), *arguments]
    if tamper == "argv":
        command[-1] = "different-bridge"
    rows = {
        "launchctl": f"{pid} 0 {launch_label}\n",
        "codesign": "",
        "ps": f"{pid} {process_uid} " + subprocess.list2cmdline(command),
    }

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[:2] == ["launchctl", "list"]:
            return SimpleNamespace(returncode=0, stdout=rows["launchctl"], stderr="")
        if argv[:2] == ["codesign", "-dv"]:
            return SimpleNamespace(
                returncode=0, stdout="", stderr="Identifier=com.ntlx.serverfs.test\n"
            )
        if argv[:2] == ["codesign", "--verify"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["ps", "-p"]:
            return SimpleNamespace(returncode=0, stdout=rows["ps"], stderr="")
        raise AssertionError(f"unexpected subprocess: {argv}")

    monkeypatch.setattr(acceptance.subprocess, "run", run)
    proof = (config, 18314 if tamper == "port" else 18313, python, {})
    if tamper == "config":
        proof = (other_config, 18313, python, {})
    assert acceptance._verified_shared_core_pid(identity, proof, pid) is (tamper == "valid")
    assert signals == []


@pytest.mark.parametrize(
    ("status", "pid", "expected"),
    [
        ("not-found", None, "inactive"),
        ("not-registered", None, "inactive"),
        ("not-found", 9123, "unknown"),
        ("not-registered", 9123, "unknown"),
        ("enabled", 9123, "registered"),
        ("enabled", None, "registered"),
        ("requires-approval", None, "registered"),
        ("undefined-status", None, "unknown"),
    ],
)
def test_native_registration_states_are_not_inferred_from_pid_only(
    monkeypatch: pytest.MonkeyPatch, status: str, pid: int | None, expected: str
) -> None:
    monkeypatch.setattr(acceptance, "_manager", lambda *_a: status)
    monkeypatch.setattr(acceptance.darwin_service, "_launchd_job_pid", lambda _label: pid)
    assert acceptance._registration_state(CORE) == expected
    assert acceptance._nothing_registered(CORE) is (expected == "inactive")


def test_native_manager_exception_remains_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args) -> str:
        raise PermissionError("test-only manager unavailable")

    monkeypatch.setattr(acceptance, "_manager", unavailable)
    monkeypatch.setattr(acceptance.darwin_service, "_launchd_job_pid", lambda _label: None)
    assert acceptance._registration_state(CORE) == "unknown"
    assert acceptance._nothing_registered(CORE) is False


@pytest.mark.parametrize("tamper", ["foreign-owner", "foreign-id", "connector", "symlink"])
def test_untrusted_synthetic_manifest_never_authorizes_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    home = tmp_path / "instances" / CORE.instance_id
    root = home / "service-manager"
    root.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    record = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": CORE.instance_id,
        "bridge_mode": "shared",
        "bridge_id": CORE.bridge_id,
        "services": {"http-origin": {}},
    }
    manifest = root / "services.json"
    if tamper == "foreign-owner":
        record["owner"] = "v0.25-named-owned-core"
    elif tamper == "foreign-id":
        record["instance_id"] = "another-test-instance"
    elif tamper == "connector":
        record["services"]["cloudflare"] = {}
    manifest.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(acceptance, "instance_service_home", lambda _identity: root)
    if tamper == "symlink":
        other = tmp_path / "other-service-manager"
        root.rename(other)
        root.symlink_to(other, target_is_directory=True)
    assert acceptance._verified_test_manifest(CORE, None) is False


@pytest.mark.parametrize(
    "tamper",
    [
        "core-port",
        "core-hash",
        "service-executable",
        "service-argv",
        "binding-secret",
        "auth-snapshot",
    ],
)
def test_pinned_synthetic_manifest_fails_closed_on_any_input_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tamper: str
) -> None:
    home = tmp_path / "instances" / CORE.instance_id
    root = home / "service-manager"
    root.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    root.chmod(0o700)
    config = tmp_path / "synthetic.toml"
    config.write_text("synthetic-config")
    python = tmp_path / "python"
    python.write_text("synthetic-executable")
    spec = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": CORE.instance_id,
        "bridge_mode": "shared",
        "bridge_id": CORE.bridge_id,
        "core": {"config": str(config), "sha256": "ab" * 32, "port": 18313},
        "services": {
            "http-origin": {
                "executable": str(python),
                "arguments": ["-m", "serverfs_mcp.cli"],
            }
        },
    }
    inputs = {
        "services.json": spec,
        "shared-bridge-binding.json": {
            "bridge_id": CORE.bridge_id,
            "core_id": CORE.instance_id,
            "secret_hex": "33" * 32,
        },
        "http-origin-environment.json": {
            "SERVERFS_MACOS_INSTANCE_ID": CORE.instance_id,
            "SERVERFS_HTTP_API_KEY": "synthetic-test",
        },
    }
    for name, document in inputs.items():
        f = root / name
        f.write_text(json.dumps(document), encoding="utf-8")
        f.chmod(0o600)
    monkeypatch.setattr(acceptance, "instance_service_home", lambda _identity: root)
    calls: list[tuple] = []

    def strict_resolver(identity, path, port, **kwargs):
        calls.append((identity, path, port, kwargs))
        return ("Workdir", tmp_path / "socket")

    monkeypatch.setattr(acceptance, "resolve_shared_core_manifest", strict_resolver)
    proof = acceptance._pin_test_candidate(CORE, config, 18313, python)
    assert acceptance._verified_test_manifest(CORE, proof)
    assert calls[-1][0] == CORE
    assert calls[-1][1:3] == (config, 18313)
    assert calls[-1][3]["expected_executable"] == python

    if tamper.startswith("core-"):
        spec["core"]["port" if tamper == "core-port" else "sha256"] = "bad"
        (root / "services.json").write_text(json.dumps(spec))
        changed = root / "services.json"
    elif tamper.startswith("service-"):
        spec["services"]["http-origin"][
            "executable" if tamper == "service-executable" else "arguments"
        ] = "bad"
        (root / "services.json").write_text(json.dumps(spec))
        changed = root / "services.json"
    elif tamper == "binding-secret":
        inputs["shared-bridge-binding.json"]["secret_hex"] = "55" * 32
        (root / "shared-bridge-binding.json").write_text(
            json.dumps(inputs["shared-bridge-binding.json"])
        )
        changed = root / "shared-bridge-binding.json"
    else:
        inputs["http-origin-environment.json"]["SERVERFS_HTTP_API_KEY"] = "changed"
        (root / "http-origin-environment.json").write_text(
            json.dumps(inputs["http-origin-environment.json"])
        )
        changed = root / "http-origin-environment.json"
    changed.chmod(0o600)
    assert acceptance._verified_test_manifest(CORE, proof) is False
    assert not changed.is_symlink()
    assert os.getuid() == changed.stat().st_uid


def test_cleanup_requires_full_strict_resolver_even_for_unchanged_pinned_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "instances" / CORE.instance_id / "service-manager"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    root.chmod(0o700)
    manifest = root / "services.json"
    manifest.write_text('{"owner":"v0.25-named-shared-core"}')
    manifest.chmod(0o600)
    for name in acceptance._INPUT_FILES[1:]:
        f = root / name
        f.write_text("{}")
        f.chmod(0o600)
    monkeypatch.setattr(acceptance, "instance_service_home", lambda _identity: root)

    def reject(*_args, **_kwargs):
        raise ValueError("synthetic strict core or owner rejection")

    monkeypatch.setattr(acceptance, "resolve_shared_core_manifest", reject)
    fingerprints = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in acceptance._INPUT_FILES
    }
    proof = (tmp_path / "config.toml", 18313, tmp_path / "python", fingerprints)
    assert acceptance._verified_test_manifest(CORE, proof) is False
