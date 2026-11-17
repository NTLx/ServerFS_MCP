"""Atomic Shared Core staging with synthetic private inputs and zero live services."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_cli
from serverfs_mcp import darwin_instance_shared_staging as staging
from serverfs_mcp.darwin_instances import APP_NAME, instance_service_home
from serverfs_mcp.instance_identity import InstanceIdentity

PORT = 18317
IDENTITY = InstanceIdentity("shared-client", "shared", "owner-bridge")


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()
    home = tmp_path / "home"
    base = home / "Library/Application Support/ServerFS"
    base.mkdir(parents=True, mode=0o700)
    base.chmod(0o700)
    workdir = tmp_path / "workspace"
    workdir.mkdir(mode=0o700)
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[agent]\nenabled = true\n[agent.codex]\nenabled = true\n"
        "[[workdirs]]\nalias = 'workspace'\n"
        f"path = '{workdir}'\nread_only = false\n"
        "agent_mode = 'workspace-write'\nagent_runtimes = ['codex']\n",
        encoding="utf-8",
    )
    inputs = tmp_path / "operator"
    inputs.mkdir(mode=0o700)
    env = inputs / "env"
    env.write_text(
        "SERVERFS_HTTP_API_KEY=synthetic-shared-core-only\n"
        f"SERVERFS_HTTP_ALLOWED_HOSTS=core.example.test,127.0.0.1:{PORT}\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    bridge_conf = inputs / "bridge.json"
    socket = tmp_path / "b.sock"
    bridge_conf.write_text(
        json.dumps(
            {
                "protocol_mode": "v2",
                "bridge_id": IDENTITY.bridge_id,
                "socket_path": str(socket),
            }
        )
    )
    bridge_conf.chmod(0o600)
    owner_binding = inputs / "owner-binding.json"
    binding = {
        "schema_version": 1,
        "bridge_id": IDENTITY.bridge_id,
        "core_id": IDENTITY.instance_id,
        "key_id": "owner-key",
        "secret_hex": "3a" * 32,
        "workdir_alias": "workspace",
        "workdir_path": str(workdir),
        "policy_revision": "f" * 64,
        "bridge_config_path": str(bridge_conf),
        "socket_path": str(socket),
    }
    owner_binding.write_text(json.dumps(binding), encoding="utf-8")
    owner_binding.chmod(0o600)
    core_python = inputs / "core-python"
    core_python.write_bytes(b"\x01synthetic-executable")
    core_python.chmod(0o700)
    template = tmp_path / "template.app"
    template.mkdir()
    calls = []

    monkeypatch.setattr(staging, "_assert_available_port", lambda *a, **k: None)
    monkeypatch.setattr(staging, "_verify_source_app", lambda *_a: None)
    # This synthetic executable is a dummy file; probe behavior is tested
    # independently against actual interpreters.
    monkeypatch.setattr(staging, "_probe_core_python_runtime", lambda *_a: None)

    def synthetic_app(identity, template_app, destination, *, shared_core_python):
        assert identity == IDENTITY
        assert template_app == template
        assert shared_core_python == core_python
        calls.append(("sign", identity.instance_id))
        destination.mkdir()
        return destination

    monkeypatch.setattr(staging, "stage_instance_app", synthetic_app)
    root = instance_service_home(IDENTITY, home=home)
    return {
        "home": home,
        "root": root,
        "config": conf,
        "env": env,
        "binding_file": owner_binding,
        "binding": binding,
        "core_python": core_python,
        "template": template,
        "workdir": workdir,
        "calls": calls,
        "bridge_conf": bridge_conf,
    }


def _stage(c: dict, *, binding_file: Path | None = None):
    return staging.stage_shared_core(
        IDENTITY,
        c["config"],
        c["env"],
        c["binding_file"] if binding_file is None else binding_file,
        PORT,
        "core.example.test",
        c["template"],
        c["core_python"],
        home=c["home"],
    )


def test_shared_stage_rejects_official_python_without_valid_provenance_before_writes(
    candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = candidate
    managed = c["config"].parent / "missing-provenance" / "bin"
    managed.mkdir(parents=True, mode=0o700)
    python = managed / "serverfs-core-python"
    python.write_bytes(b"synthetic-tampered-runtime")
    python.chmod(0o700)
    with pytest.raises(staging.DarwinSharedStageError, match="integrity"):
        staging.stage_shared_core(
            IDENTITY,
            c["config"],
            c["env"],
            c["binding_file"],
            PORT,
            "core.example.test",
            c["template"],
            python,
            home=c["home"],
        )
    assert not c["root"].exists()
    assert c["calls"] == []


def test_shared_stage_creates_signed_candidate_without_provisioning_owner(candidate) -> None:
    c = candidate
    before = c["binding_file"].read_bytes()
    result = _stage(c)
    assert result == {
        "instance_id": IDENTITY.instance_id,
        "bridge_id": IDENTITY.bridge_id,
        "bridge_mode": "shared",
        "core_port": PORT,
        "state": "staged-not-registered",
    }
    root = c["root"]
    for key in ("services.json", "http-origin-environment.json", "shared-bridge-binding.json"):
        item = root / key
        assert item.is_file()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (root / APP_NAME).is_dir()
    assert set(json.loads((root / "services.json").read_text())["services"]) == {"http-origin"}
    document = json.loads((root / "services.json").read_text())
    assert document["services"]["http-origin"]["executable"] == str(c["core_python"])
    assert document["services"]["http-origin"]["arguments"][:2] == ["-B", "-m"]
    assert document["services"]["http-origin"]["arguments"][-2:] == [
        "--macos-shared-bridge",
        IDENTITY.bridge_id,
    ]
    assert (
        json.loads((root / "http-origin-environment.json").read_text())[
            "SERVERFS_MACOS_INSTANCE_ID"
        ]
        == IDENTITY.instance_id
    )
    assert c["binding_file"].read_bytes() == before
    assert (root / "shared-bridge-binding.json").read_bytes() == before
    assert not (root / "data/agent-bridge").exists()
    assert c["calls"] == [("sign", IDENTITY.instance_id)]


@pytest.mark.parametrize("name", ["wrong-core", "wrong-bridge", "wrong-alias", "bad-secret"])
def test_shared_stage_rejects_invalid_grant_and_removes_new_candidate(candidate, name: str) -> None:
    c = candidate
    document = dict(c["binding"])
    if name == "wrong-core":
        document["core_id"] = "another-core"
    elif name == "wrong-bridge":
        document["bridge_id"] = "another-bridge"
    elif name == "wrong-alias":
        document["workdir_alias"] = "other"
    else:
        document["secret_hex"] = "invalid-secret"
    c["binding_file"].write_text(json.dumps(document))
    c["binding_file"].chmod(0o600)
    with pytest.raises((staging.DarwinSharedStageError, ValueError)):
        _stage(c)
    assert not c["root"].exists()
    assert not (c["root"].parent).exists()
    assert c["binding_file"].exists()
    assert c["workdir"].exists()


def test_shared_stage_rejects_existing_instance_and_does_not_replace(candidate) -> None:
    c = candidate
    result = _stage(c)
    original = (c["root"] / "services.json").read_bytes()
    with pytest.raises(staging.DarwinSharedStageError, match="already exists"):
        _stage(c)
    assert result["state"] == "staged-not-registered"
    assert (c["root"] / "services.json").read_bytes() == original


def test_shared_stage_rejects_public_binding_before_creating_any_state(candidate) -> None:
    c = candidate
    c["binding_file"].chmod(0o644)
    with pytest.raises(staging.DarwinSharedStageError):
        _stage(c)
    assert not c["root"].exists()


def test_shared_stage_rejects_unsafe_environment_before_creating_any_state(candidate) -> None:
    c = candidate
    c["env"].write_text(
        f"SERVERFS_HTTP_ALLOWED_HOSTS=wrong.example.test,127.0.0.1:{PORT}\n"
        "SERVERFS_HTTP_API_KEY=synthetic\n"
    )
    with pytest.raises(staging.DarwinSharedStageError):
        _stage(c)
    assert not c["root"].exists()


def test_shared_stage_is_fail_closed_if_app_signing_fails(
    candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = candidate

    def fail_sign(*_args, **_kwargs):
        raise ValueError("synthetic signing error")

    monkeypatch.setattr(staging, "stage_instance_app", fail_sign)
    with pytest.raises(ValueError, match="synthetic signing error"):
        _stage(c)
    assert not c["root"].exists()
    assert c["binding_file"].exists()
    assert c["bridge_conf"].exists()


def test_shared_stage_requires_shared_named_identity_without_side_effects(candidate) -> None:
    c = candidate
    for identity in (
        InstanceIdentity(),
        InstanceIdentity("shared-client", "disabled"),
        InstanceIdentity("shared-client", "owned", "shared-client"),
    ):
        with pytest.raises(staging.DarwinSharedStageError):
            staging.stage_shared_core(
                identity,
                c["config"],
                c["env"],
                c["binding_file"],
                PORT,
                "core.example.test",
                c["template"],
                c["core_python"],
                home=c["home"],
            )
    assert not c["root"].exists()


def test_shared_stage_cli_routes_only_explicit_owner_binding(
    candidate, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    c = candidate
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    captured = []
    monkeypatch.setattr(
        staging,
        "stage_shared_core",
        lambda *args: captured.append(args) or {"state": "staged-not-registered"},
    )
    args = [
        "--instance",
        IDENTITY.instance_id,
        "shared-stage",
        "--bridge-id",
        IDENTITY.bridge_id,
        "--config",
        str(c["config"]),
        "--env-file",
        str(c["env"]),
        "--binding-file",
        str(c["binding_file"]),
        "--http-port",
        str(PORT),
        "--hostname",
        "core.example.test",
        "--template-app",
        str(c["template"]),
        "--core-python",
        str(c["core_python"]),
    ]
    assert darwin_instance_cli.main(args) == 0
    assert captured and captured[0][0] == IDENTITY
    assert len(captured[0]) == 8
    assert capsys.readouterr().err == ""


def test_exact_staged_manifest_is_accepted_by_native_swift_gate(candidate, tmp_path: Path) -> None:
    """Ensure the real staging output, not a handwritten sample, satisfies Swift."""
    import shutil
    import subprocess

    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("Swift compiler is unavailable")
    _stage(candidate)
    root = candidate["root"]
    harness = tmp_path / "main.swift"
    harness.write_text(
        """
import Foundation
let raw = FileHandle.standardInput.readDataToEndOfFile()
do {
    _ = try validateSharedManifestGate(
        rawManifest: raw, instanceID: CommandLine.arguments[2],
        expectedServiceHome: URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    )
    print("accepted")
} catch {
    print("rejected")
}
""",
        encoding="utf-8",
    )
    source = (
        Path(__file__).resolve().parents[1]
        / "native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift"
    )
    binary = tmp_path / "staged-swift-gate"
    compiled = subprocess.run(
        [swiftc, str(source), str(harness), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run(
        [str(binary), str(root), IDENTITY.instance_id],
        input=(root / "services.json").read_bytes(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == "accepted"


def test_probe_runs_exact_interpreter_without_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("SERVERFS_HTTP_API_KEY", "must-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/site-packages")
    observed: dict = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(staging.subprocess, "run", fake_run)
    staging._probe_core_python_runtime(tmp_path / "python", tmp_path)
    assert observed["argv"][:4] == [str(tmp_path / "python"), "-I", "-B", "-c"]
    assert "serverfs_mcp.cli" in observed["argv"][4]
    assert "mcp" in observed["argv"][4]
    assert "PYTHONPATH" not in observed["env"]
    assert "SERVERFS_HTTP_API_KEY" not in observed["env"]
    assert observed["timeout"] == 15
    assert observed["cwd"] == tmp_path
    assert observed["stdin"] == staging.subprocess.DEVNULL
    assert observed["stdout"] == staging.subprocess.DEVNULL
    assert observed["stderr"] == staging.subprocess.DEVNULL


@pytest.mark.parametrize("failure", ["module-not-found", "timeout", "os-error"])
def test_probe_refuses_missing_runtime_without_leaking_error_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from types import SimpleNamespace

    def fake_run(*_args, **_kwargs):
        if failure == "timeout":
            raise staging.subprocess.TimeoutExpired("synthetic secret", timeout=15)
        if failure == "os-error":
            raise OSError("synthetic secret")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(staging.subprocess, "run", fake_run)
    with pytest.raises(staging.DarwinSharedStageError) as caught:
        staging._probe_core_python_runtime(tmp_path / "python", tmp_path)
    assert "synthetic secret" not in str(caught.value)
    assert "cannot load" in str(caught.value)


def test_shared_stage_refuses_missing_python_deps_before_any_state(
    candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = candidate

    def unavailable(*_args):
        raise staging.DarwinSharedStageError(
            "shared Core Python cannot load the isolated ServerFS runtime"
        )

    monkeypatch.setattr(staging, "_probe_core_python_runtime", unavailable)
    with pytest.raises(staging.DarwinSharedStageError, match="cannot load"):
        _stage(c)
    assert not c["root"].exists()
    assert c["binding_file"].exists()
    assert c["calls"] == []
