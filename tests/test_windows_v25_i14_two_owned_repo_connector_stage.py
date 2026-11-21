"""I14 source-only regression: independent Repo-only public Connector staging.

These tests never register tasks, read Tokens, call Cloudflare or start a Core.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "deployment/windows/v0250-i14-two-owned-repo-public-connector-stage.py"
PRE = ROOT / "deployment/windows/v0250-i13-owned-repo-preflight.py"


def load(path: Path, label: str):
    spec = importlib.util.spec_from_file_location(label, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_two_separate_core_tunnels_and_public_routes() -> None:
    s = load(STAGE, "i14_public_exact")
    assert s.ROUTES == (
        ("i13-a", "i14-edge-a", "i7-edge-a", 18883, "workpc-v25a.ntlx.top"),
        ("i13-b", "i14-edge-b", "i7-edge-b", 18884, "workpc-v25b.ntlx.top"),
    )
    assert len({row[0] for row in s.ROUTES}) == 2
    assert len({row[1] for row in s.ROUTES}) == 2
    assert len({row[2] for row in s.ROUTES}) == 2
    assert len({row[3] for row in s.ROUTES}) == 2
    assert len({row[4] for row in s.ROUTES}) == 2


def test_i13_gate_accepts_only_exact_four_or_exact_six_disabled_tasks() -> None:
    pre = load(PRE, "i14_optional_native_task_inventory")
    assert pre.OPTIONAL_CONNECTOR_NAMES == {
        "ServerFS v25 i13-a Cloudflare Connector i14-edge-a",
        "ServerFS v25 i13-b Cloudflare Connector i14-edge-b",
    }
    base = [{"name": name, "state": "Disabled", "enabled": False} for name in pre.TASK_NAMES]
    extra = [
        {"name": name, "state": "Disabled", "enabled": False}
        for name in pre.OPTIONAL_CONNECTOR_NAMES
    ]
    pre.verify_tasks(base, installed=True)
    pre.verify_tasks(base + extra, installed=True)
    pre.verify_tasks([], installed=False)
    for invalid in (
        base + extra[:1],
        base + extra + [extra[0]],
        base
        + extra
        + [{"name": "ServerFS v25 i13-other Core", "enabled": False, "state": "Disabled"}],
        base + [{**extra[0], "enabled": True, "state": "Ready"}, extra[1]],
    ):
        with pytest.raises(pre.I13PreflightRefusal):
            pre.verify_tasks(invalid, installed=True)
    with pytest.raises(pre.I13PreflightRefusal):
        pre.verify_tasks(base, installed=False)


def test_stage_is_one_use_safely_private_disabled_and_no_remote_mutation() -> None:
    source = STAGE.read_text("utf-8")
    for required in (
        "i13.postflight()",
        "i7.postflight()",
        "i7.private_token_record(source, sid=sid)",
        "installer._new_private_file(destination, data, sid)",
        "i7.private_token_record(destination, sid=sid)",
        'storage.prepare("cloudflare", core, connector)',
        '"instance_id": core',
        '"connector_id": connector',
        '"hostname": core + ".local"',
        '"port": port',
        "Scheduler().install(manifest)",
        "storage.stage(manifest)",
        'state.get("readiness") == "offline"',
        "i13pre.OPTIONAL_CONNECTOR_NAMES",
        "I14_READONLY_STAGE_PREFLIGHT=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "urllib.request",
        "requests.post(",
        "subprocess.Popen(",
        "os.system(",
        "shell=True",
        "git push",
        "git commit",
        "print(data)",
        "print(token)",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source, forbidden


def test_stage_disables_before_start_without_running_external_integration() -> None:
    source = STAGE.read_text("utf-8")
    tree = ast.parse(source)
    functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    assert {"baseline", "native", "fresh", "stage", "postflight", "main"} <= functions.keys()
    calls = [c for c in ast.walk(functions["stage"]) if isinstance(c, ast.Call)]
    attrs = [c.func.attr for c in calls if isinstance(c.func, ast.Attribute)]
    assert attrs.count("install") == 1
    assert "start" not in attrs and "stop" not in attrs and "uninstall" not in attrs
    body = ast.get_source_segment(source, functions["stage"])
    assert body is not None and body.index("fresh()") < body.index('storage.prepare("cloudflare"')


def test_each_core_has_its_own_native_pinned_connector_venv_and_binary() -> None:
    source = STAGE.read_text("utf-8")
    for expected in (
        'installer._new_dir(base / "build" / SLOT, sid)',
        'installer._new_dir(base / "services/cloudflare/bin", sid)',
        "installed._installed_core(",
        'venv=base / "envs/cloudflare"',
        "shutil.copyfile(binary, destination)",
        'destination = base / "services/cloudflare/bin/cloudflared.exe"',
        '"pythonw": str(base / "envs/cloudflare/Scripts/pythonw.exe")',
        '"cloudflared": str(base / "services/cloudflare/bin/cloudflared.exe")',
        "pin.verify_native_wheel(native_wheel)",
        "installer._protect_tree(build, sid)",
        "installer._protect_tree(destination.parent, sid)",
        "target.read_bytes() == source.read_bytes()",
    ):
        assert expected in source, expected
    assert '"pythonw": str(owner / "Scripts/pythonw.exe")' not in source
    assert '"cloudflared": str(binary)' not in source


def test_bounded_refusal_without_secret_output() -> None:
    s = load(STAGE, "i14_private_refusal")
    s.require(True, "ok")
    with pytest.raises(s.I14StageRefusal):
        s.require(False, "no")


def test_i13_original_four_task_guard_still_requires_clean_install_namespace() -> None:
    pre = load(PRE, "i14_original_i13_preflight")
    assert len(pre.TASK_NAMES) == 4
    assert not (pre.TASK_NAMES & pre.OPTIONAL_CONNECTOR_NAMES)
    assert "verify_tasks(task_rows(), installed=False)" in PRE.read_text("utf-8")
