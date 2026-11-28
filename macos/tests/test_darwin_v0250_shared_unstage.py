"""Shared Core unstage must be scoped, inactive, pristine, and Bridge-independent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_cli
from serverfs_mcp import darwin_instance_shared_unstage as unstage
from serverfs_mcp.darwin_instances import instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

IDENTITY = InstanceIdentity("core-test", "shared", "owner-bridge")


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path.resolve() / "home"
    root = instance_service_home(IDENTITY, home=home)
    root.mkdir(parents=True, mode=0o700)
    for folder in (root.parent.parent, root.parent, root):
        folder.chmod(0o700)
    (root / "logs").mkdir(mode=0o700)
    (root / "data").mkdir(mode=0o700)
    (root / "ServerFSServiceManager.app").mkdir(mode=0o700)
    manifest = {
        "schema_version": 1,
        "owner": "v0.25-named-shared-core",
        "instance_id": IDENTITY.instance_id,
        "bridge_mode": "shared",
        "bridge_id": IDENTITY.bridge_id,
        "services": {"http-origin": {}},
    }
    for name, value in (
        ("services.json", manifest),
        ("shared-bridge-binding.json", {"secret_hex": "3c" * 32}),
        ("http-origin-environment.json", {"SERVERFS_HTTP_API_KEY": "unit-only"}),
    ):
        item = root / name
        item.write_text(json.dumps(value))
        item.chmod(0o600)
    state = {"status": "not-registered", "present": False, "app_valid": True}
    calls = []

    def manager(_identity, operation, component, *, home=None):
        assert _identity == IDENTITY
        assert component == "http-origin"
        assert operation == "status"
        calls.append(("status", component))
        return state["status"]

    def app(_identity, _home=None):
        calls.append(("signed-app",))
        if not state["app_valid"]:
            raise ValueError("signed app failed validation")
        return root / "ServerFSServiceManager.app/serverfs-service-manager"

    monkeypatch.setattr(unstage, "_manager", manager)
    monkeypatch.setattr(unstage, "_manager_path", app)
    monkeypatch.setattr(
        unstage.darwin_service,
        "_launchd_job_present",
        lambda _: state["present"],
    )
    return {"root": root, "home": home, "state": state, "calls": calls}


def test_unstage_removes_only_inactive_pristine_shared_core(staged) -> None:
    c = staged
    other = c["root"].parent.parent / "unrelated-bridge-owner"
    other.mkdir(mode=0o700)
    (other / "do-not-remove").write_text("preserve")
    peer = instance_service_home(
        InstanceIdentity("peer-core", "shared", "owner-bridge"), home=c["home"]
    )
    peer.mkdir(parents=True, mode=0o700)
    (peer / "peer-marker").write_text("preserve")
    default = instance_service_home(InstanceIdentity("default", "disabled", None), home=c["home"])
    default.mkdir(parents=True, mode=0o700)
    (default / "default-marker").write_text("preserve")
    result = unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert result == {"instance_id": "core-test", "bridge_mode": "shared", "state": "unstaged"}
    assert not c["root"].parent.exists()
    assert (other / "do-not-remove").read_text() == "preserve"
    assert (peer / "peer-marker").read_text() == "preserve"
    assert (default / "default-marker").read_text() == "preserve"
    assert c["calls"] == [
        ("status", "http-origin"),
        ("signed-app",),
        ("status", "http-origin"),
    ]


def test_unstage_refuses_contended_shared_start_lock_before_trust_checks(staged) -> None:
    fcntl = pytest.importorskip("fcntl")
    manifest = staged["root"] / "services.json"
    fd = os.open(manifest, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(unstage.DarwinSharedUnstageError, match="start lock"):
            unstage.unstage_shared_core(IDENTITY, home=staged["home"])
        assert staged["calls"] == []
        assert manifest.exists()
        assert (staged["root"] / "ServerFSServiceManager.app").exists()
    finally:
        os.close(fd)


def test_unstage_fails_closed_if_locked_manifest_inode_changes(staged) -> None:
    manifest = staged["root"] / "services.json"
    original = manifest.read_bytes()

    def replace_during_signature_check(_identity, _home=None):
        replacement = manifest.with_suffix(".replacement")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        replacement.replace(manifest)
        return staged["root"] / "ServerFSServiceManager.app/serverfs-service-manager"

    staged["calls"].clear()
    from unittest.mock import patch

    with patch.object(unstage, "_manager_path", replace_during_signature_check):
        with pytest.raises(unstage.DarwinSharedUnstageError, match="start lock"):
            unstage.unstage_shared_core(IDENTITY, home=staged["home"])
    assert manifest.exists()
    assert (staged["root"] / "shared-bridge-binding.json").exists()
    assert (staged["root"] / "ServerFSServiceManager.app").exists()


@pytest.mark.parametrize("status", ["enabled", "requires-approval"])
def test_unstage_refuses_registered_core(staged, status: str) -> None:
    c = staged
    c["state"]["status"] = status
    with pytest.raises(unstage.DarwinSharedUnstageError, match="unregister"):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["root"].exists()


def test_unstage_refuses_present_core_job(staged) -> None:
    c = staged
    c["state"]["present"] = True
    with pytest.raises(unstage.DarwinSharedUnstageError, match="process"):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["root"].exists()


@pytest.mark.parametrize(
    "path",
    ["logs/debug.log", "data/task-state.db", "unrecognized", "data/owner-bridge"],
)
def test_unstage_refuses_runtime_data_or_unknown_files(staged, path: str) -> None:
    c = staged
    item = c["root"] / path
    item.parent.mkdir(parents=True, exist_ok=True)
    item.write_text("retained")
    with pytest.raises(unstage.DarwinSharedUnstageError):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert item.read_text() == "retained"


def test_unstage_refuses_damaged_app_signature(staged) -> None:
    c = staged
    c["state"]["app_valid"] = False
    with pytest.raises(ValueError, match="signed app"):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert (c["root"] / "services.json").exists()


@pytest.mark.parametrize("bad_status", [[], {}, None, "enabled"])
@pytest.mark.parametrize("query", [1, 2])
def test_unstage_fails_closed_on_bad_manager_state(staged, monkeypatch, bad_status, query):
    c = staged
    calls = 0

    def manager(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == query:
            return bad_status
        return "not-registered"

    monkeypatch.setattr(unstage, "_manager", manager)
    with pytest.raises(unstage.DarwinSharedUnstageError):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["root"].exists()
    assert (c["root"] / "ServerFSServiceManager.app").exists()


@pytest.mark.parametrize("query", [1, 2])
def test_unstage_fails_closed_on_manager_exception(staged, monkeypatch, query):
    c = staged
    calls = 0

    def manager(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == query:
            raise RuntimeError("sensitive manager detail")
        return "not-registered"

    monkeypatch.setattr(unstage, "_manager", manager)
    with pytest.raises(unstage.DarwinSharedUnstageError) as caught:
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert "sensitive" not in str(caught.value)
    assert c["root"].exists()


@pytest.mark.parametrize("query", [1, 2])
def test_unstage_fails_closed_on_presence_exception(staged, monkeypatch, query):
    c = staged
    calls = 0

    def present(_label):
        nonlocal calls
        calls += 1
        if calls == query:
            raise RuntimeError("sensitive launchd detail")
        return False

    monkeypatch.setattr(unstage.darwin_service, "_launchd_job_present", present)
    with pytest.raises(unstage.DarwinSharedUnstageError) as caught:
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert "sensitive" not in str(caught.value)
    assert c["root"].exists()


@pytest.mark.parametrize("bad_presence", [[], {}, 0, ""])
@pytest.mark.parametrize("query", [1, 2])
def test_unstage_fails_closed_on_malformed_presence(staged, monkeypatch, bad_presence, query):
    c = staged
    calls = 0

    def present(_label):
        nonlocal calls
        calls += 1
        if calls == query:
            return bad_presence
        return False

    monkeypatch.setattr(unstage.darwin_service, "_launchd_job_present", present)
    with pytest.raises(unstage.DarwinSharedUnstageError):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["root"].exists()


@pytest.mark.parametrize("drift", ["registered", "present"])
def test_unstage_rechecks_state_after_signed_app_validation(staged, monkeypatch, drift):
    c = staged

    def app(_identity, _home=None):
        c["calls"].append(("signed-app",))
        if drift == "registered":
            c["state"]["status"] = "requires-approval"
        else:
            c["state"]["present"] = True
        return c["root"] / "ServerFSServiceManager.app/serverfs-service-manager"

    # The fixture's app validator normally provides the signature gate. Replace
    # it with a deterministic state transition during that validation window.
    monkeypatch.setattr(unstage, "_manager_path", app)
    with pytest.raises(unstage.DarwinSharedUnstageError):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["calls"] == [
        ("status", "http-origin"),
        ("signed-app",),
        ("status", "http-origin"),
    ]
    assert c["root"].exists()
    assert (c["root"] / "services.json").exists()
    assert (c["root"] / "ServerFSServiceManager.app").exists()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "state = waiting\n", "", True),
        (0, "pid = 4096\n", "", True),
        (113, "", 'Could not find service "label"\n', False),
        (113, "", 'Bad request.\nCould not find service "label"\n', False),
    ],
)
def test_launchd_job_present_classifies_completed_process(
    monkeypatch, returncode, stdout, stderr, expected
):
    completed = subprocess.CompletedProcess(
        ["launchctl", "print", "gui/501/label"], returncode, stdout, stderr
    )
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return completed

    monkeypatch.setattr(unstage.darwin_service.subprocess, "run", run)
    assert unstage.darwin_service._launchd_job_present("label") is expected
    assert calls[0][0] == ["launchctl", "print", f"gui/{unstage.darwin_service.os.getuid()}/label"]
    assert calls[0][1] == {
        "capture_output": True,
        "text": True,
        "timeout": 10,
        "check": False,
    }


@pytest.mark.parametrize(
    "completed",
    [
        subprocess.CompletedProcess(["launchctl"], 113, "unknown", "Could not find service"),
        subprocess.CompletedProcess(["launchctl"], 113, "", "other failure"),
        subprocess.CompletedProcess(["launchctl"], 113, "", "prefix\nCould not find service label"),
        subprocess.CompletedProcess(
            ["launchctl"], 113, "", "unexpected\nBad request.\nCould not find service label"
        ),
        subprocess.CompletedProcess(["launchctl"], 113, "", "Bad request.\nunrelated error"),
        subprocess.CompletedProcess(
            ["launchctl"], 113, "", "Bad request.\nCould not find service label\nunexpected"
        ),
        subprocess.CompletedProcess(["launchctl"], 1, "", 'Could not find service "label"'),
        subprocess.CompletedProcess(["launchctl"], 1, "", ""),
        subprocess.CompletedProcess(["launchctl"], 0, None, ""),
        None,
    ],
)
def test_launchd_job_present_fails_closed_on_ambiguous_result(monkeypatch, completed):
    monkeypatch.setattr(
        unstage.darwin_service.subprocess, "run", lambda *_args, **_kwargs: completed
    )
    with pytest.raises(
        unstage.darwin_service.DarwinServiceError,
        match="^launchd job presence unavailable$",
    ):
        unstage.darwin_service._launchd_job_present("label")


def test_launchd_job_present_sanitizes_invocation_exception(monkeypatch):
    def run(*_args, **_kwargs):
        raise OSError("private path detail")

    monkeypatch.setattr(unstage.darwin_service.subprocess, "run", run)
    with pytest.raises(
        unstage.darwin_service.DarwinServiceError,
        match="^launchd job presence unavailable$",
    ) as caught:
        unstage.darwin_service._launchd_job_present("label")
    assert "private path" not in str(caught.value)


def test_unstage_refuses_owner_drift(staged) -> None:
    c = staged
    manifest = c["root"] / "services.json"
    content = json.loads(manifest.read_text())
    content["bridge_id"] = "unexpected-owner"
    manifest.write_text(json.dumps(content))
    manifest.chmod(0o600)
    with pytest.raises(unstage.DarwinSharedUnstageError, match="identity|untrusted"):
        unstage.unstage_shared_core(IDENTITY, home=c["home"])
    assert c["root"].exists()


def test_cli_unstage_requires_explicit_shared_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(
        unstage,
        "unstage_shared_core",
        lambda identity: seen.append(identity) or {"state": "unstaged"},
    )
    assert (
        darwin_instance_cli.main(
            ["--instance", "core-test", "shared-unstage", "--bridge-id", "owner-bridge"]
        )
        == 0
    )
    assert seen == [IDENTITY]
    assert capsys.readouterr().err == ""
