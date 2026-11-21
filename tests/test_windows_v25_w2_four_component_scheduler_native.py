"""Opt-in Win32 Task Scheduler registration of four independent v25 component contracts.

All tasks are named for a fresh, random test-only non-default instance. They
remain disabled, are NEVER started, and are deleted only after ownership,
identity, command, SID and disabled-state checks. No production task names,
service home, credentials, tunnel, provider or repo mutation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import pytest

from serverfs_mcp.native_endpoint import current_user_sid
from serverfs_mcp.windows_component_task import NAMESPACE, build_xml, validate_registered
from serverfs_mcp.windows_service import _decode_process_output, _schtasks_executable
from serverfs_mcp.windows_v25 import tasks
from serverfs_mcp.windows_v25.spec import COMPONENTS, Manifest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Scheduler only"),
    pytest.mark.skipif(
        os.environ.get("SERVERFS_W25_FOUR_COMPONENT_SCHEDULER") != "1",
        reason="explicit isolated four-component Scheduler registration opt-in required",
    ),
]

_MISSING = frozenset({0x80070002, 0x80070003})
_NS = {"t": NAMESPACE}


def _call(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(_schtasks_executable()), *args],
        capture_output=True,
        check=False,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _read(name: str) -> str | None:
    result = _call("/Query", "/TN", name, "/XML", "/HResult")
    if result.returncode == 0:
        return _decode_process_output(result.stdout).lstrip("\ufeff\x00 \r\n\t")
    if result.returncode & 0xFFFFFFFF in _MISSING:
        return None
    pytest.fail("test-owned Scheduler query could not determine task identity")


def _disabled(xml: str) -> bool:
    tree = ET.fromstring(xml)
    node = tree.find("./t:Settings/t:Enabled", _NS)
    return node is not None and (node.text or "").strip().lower() in ("false", "0")


def _manifest(kind: str, instance_id: str, pythonw: Path, tmp_path: Path) -> Manifest:
    base = tmp_path / instance_id
    if kind == "core":
        settings = {
            "config": str(base / "config.toml"),
            "auth_file": str(base / "auth.env"),
            "port": 49877,
            "hostname": "core.w25.fixture",
        }
    elif kind == "bridge":
        settings = {
            "config": str(base / "bridge.json"),
            "bridge_python": str(pythonw.with_name("python.exe")),
        }
    elif kind == "cloudflare":
        settings = {
            "cloudflared": str(base / "cloudflared.exe"),
            "token_file": str(base / "cloudflare.token"),
            "port": 49877,
            "hostname": "core.w25.fixture",
        }
    else:
        settings = {
            "tunnel_client": str(base / "tunnel-client.exe"),
            "tunnel_id": "tunnel_" + "a" * 32,
            "control_plane_key_file": str(base / "control-key.env"),
            "core_auth_header_file": str(base / "core-header.env"),
            "port": 49877,
            "hostname": "core.w25.fixture",
        }
    raw = {
        "schema": 2,
        "component": kind,
        "instance_id": instance_id,
        "sid": current_user_sid(),
        "pythonw": str(pythonw),
        "settings": settings,
    }
    if kind in ("cloudflare", "openai"):
        raw["connector_id"] = "edge-cf" if kind == "cloudflare" else "edge-oa"
    return Manifest.parse(raw)


def test_four_independent_v25_components_register_disabled_without_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use only the EXISTING test interpreter. Nothing is launched via Scheduler.
    pythonw = Path(sys.executable).with_name("pythonw.exe").resolve(strict=True)
    instance_id = "w25gate" + uuid4().hex[:16]
    manifests = tuple(
        _manifest(kind, instance_id, pythonw, tmp_path)
        for kind in ("core", "bridge", "cloudflare", "openai")
    )
    # Owner code must generate the exact real v25 argv and task identity, but
    # pin the never-executed fixture action to an available disposable venv.
    monkeypatch.setattr(tasks.storage, "pinned_pythonw", lambda *_args: pythonw)
    specs = tuple(tasks._task_spec(manifest) for manifest in manifests)
    assert len({spec.name for spec in specs}) == 4
    assert not {spec.name for spec in specs}.intersection(COMPONENTS.values())
    assert all(instance_id in spec.name for spec in specs)
    assert all(
        spec.arguments[:4] == ("-I", "-B", "-m", "serverfs_mcp.windows_v25.runtime")
        for spec in specs
    )
    assert len({spec.arguments for spec in specs}) == 4

    attempted = []
    try:
        # Preflight ALL names before any mutation, even though test IDs are random.
        for spec in specs:
            assert _read(spec.name) is None, "test task name unexpectedly occupied"
        for spec in specs:
            authored = tmp_path / (spec.name + ".xml")
            authored.write_bytes(build_xml(spec))
            attempted.append(spec)
            result = _call("/Create", "/TN", spec.name, "/XML", str(authored))
            if result.returncode:
                pytest.fail("test-only disabled component registration was refused")
            observed = _read(spec.name)
            assert observed is not None
            valid, reason = validate_registered(observed, spec)
            assert valid, reason
            assert _disabled(observed), "test component unexpectedly enabled"
        # All four must coexist; a peer's XML must not qualify for this owner.
        for own in specs:
            xml = _read(own.name)
            assert xml is not None and _disabled(xml)
            assert validate_registered(xml, own)[0]
            for peer in specs:
                if peer is not own:
                    assert not validate_registered(xml, peer)[0]
    finally:
        # NEVER delete a foreign/altered/enabled task or a production name.
        for spec in reversed(attempted):
            observed = _read(spec.name)
            if observed is None:
                continue
            valid, _reason = validate_registered(observed, spec)
            if not valid or not _disabled(observed):
                pytest.fail("test-only task owner proof failed: cleanup refused")
            result = _call("/Delete", "/TN", spec.name, "/F")
            assert result.returncode == 0, "owned disabled fixture task cleanup failed"
            assert _read(spec.name) is None
