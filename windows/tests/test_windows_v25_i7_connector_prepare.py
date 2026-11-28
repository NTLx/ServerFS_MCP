"""I7 phase-2 safely prepares, never enrolls, two Cloudflare Connector identities."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i7-connector-prepare.py"
_SPEC = importlib.util.spec_from_file_location("i7_connector_prepare_contract", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
prepare = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prepare)


def test_fixed_identity_and_nocredential_lifecycle_boundaries() -> None:
    assert prepare.INSTANCE == "i7-public"
    assert prepare.CONNECTORS == ("i7-edge-a", "i7-edge-b")
    text = _SOURCE.read_text(encoding="utf-8")
    for mandatory in (
        "phase1.postflight()",
        "phase1.protected_baseline()",
        "storage.prepare(",
        "i3._installed_core(",
        "installer._wheel(",
        "storage.check_private(",
        "native_pin.verify_native_wheel(",
        "I7_NO_CONNECTOR_TOKENS_MANIFESTS_OR_TASKS=PASS",
    ):
        assert mandatory in text
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().install(",
        "storage.stage(",
        "uv pip install --index",
        "cloudflare.request(",
        "subprocess.Popen(",
        "token_file",
        "secret_hex",
        "get_tunnel_token",
        "git push",
    ):
        assert forbidden not in text


def test_native_scheduler_check_requires_exact_zero_connector_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"I7_CONNECTORS_ABSENT"),
    )
    prepare.candidates_absent()
    monkeypatch.setattr(
        prepare.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=13, stdout=b""),
    )
    with pytest.raises(prepare.I7ConnectorPrepareRefusal, match="Scheduler identity occupied"):
        prepare.candidates_absent()


def test_audit_fails_closed_on_preexisting_connector_or_nonempty_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serverfs_mcp.windows_v25 import storage

    (tmp_path / "envs/cloudflare").mkdir(parents=True)
    (tmp_path / "services/cloudflare/connectors").mkdir(parents=True)
    monkeypatch.setattr(prepare, "host", lambda: None)
    monkeypatch.setattr(prepare, "_base_core", lambda: None)
    monkeypatch.setattr(prepare, "candidates_absent", lambda: None)
    monkeypatch.setattr(storage, "root", lambda instance_id: tmp_path)
    monkeypatch.setattr(
        storage,
        "home",
        lambda component, instance_id, connector_id: (
            tmp_path / "services/cloudflare/connectors" / connector_id
        ),
    )
    monkeypatch.setattr(storage, "_sid", lambda: "fake-sid")
    monkeypatch.setattr(storage, "check_private", lambda *a, **k: None)
    prepare.audit()
    (tmp_path / "envs/cloudflare/pyvenv.cfg").write_text("occupied", encoding="utf-8")
    with pytest.raises(prepare.I7ConnectorPrepareRefusal, match="not fresh"):
        prepare.audit()
    (tmp_path / "envs/cloudflare/pyvenv.cfg").unlink()
    (tmp_path / "services/cloudflare/connectors/i7-edge-a").mkdir()
    with pytest.raises(prepare.I7ConnectorPrepareRefusal, match="already occupies"):
        prepare.audit()


def test_postflight_refuses_any_staged_token_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serverfs_mcp.windows_v25 import storage

    base = tmp_path
    owner = base / "envs/cloudflare"
    homes = base / "services/cloudflare/connectors"
    for dirname in (
        "envs/cloudflare/Scripts",
        "envs/cloudflare/Lib/site-packages/serverfs_mcp/windows_v25",
        "build/cloudflare-owner",
        "services/cloudflare/connectors/i7-edge-a",
        "services/cloudflare/connectors/i7-edge-b",
    ):
        (base / dirname).mkdir(parents=True, exist_ok=True)
    for exe in ("python.exe", "pythonw.exe"):
        (owner / "Scripts" / exe).write_bytes(b"fixture")
    for relative in (
        "windows_v25/runtime.py",
        "windows_v25/storage.py",
        "windows_v25/cli.py",
        "windows_v25/spec.py",
        "windows_v25/readiness.py",
        "windows_v25/isolation.py",
    ):
        target = owner / "Lib/site-packages/serverfs_mcp" / relative
        target.write_bytes((prepare.REPO / "src/serverfs_mcp" / relative).read_bytes())

    monkeypatch.setattr(prepare, "host", lambda: None)
    monkeypatch.setattr(prepare, "_installed_core_and_binary", lambda: None)
    monkeypatch.setattr(prepare, "candidates_absent", lambda: None)
    monkeypatch.setattr(storage, "root", lambda instance_id: base)
    monkeypatch.setattr(
        storage,
        "home",
        lambda component, instance_id, connector_id: homes / connector_id,
    )
    monkeypatch.setattr(storage, "_sid", lambda: "fixture-sid")
    monkeypatch.setattr(storage, "check_private", lambda *a, **k: None)
    monkeypatch.setattr(storage, "component_inventory", lambda *a: ())
    prepare.postflight()

    (homes / "i7-edge-a" / "tunnel.token").write_text("test-only", encoding="utf-8")
    with pytest.raises(prepare.I7ConnectorPrepareRefusal, match="must contain no token"):
        prepare.postflight()


def test_python_argv_has_no_shell_or_tunnel_execution() -> None:
    text = _SOURCE.read_text(encoding="utf-8")
    assert "capture_output=True" in text
    assert 'creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)' in text
    assert "subprocess.run(" in text
    assert "cloudflared.exe" in text
    assert "run_connector(" not in text
