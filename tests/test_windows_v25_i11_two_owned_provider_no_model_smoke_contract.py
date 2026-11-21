"""I11 two owned Provider-ready pairs no-model native E2E source contract.

Never starts an installed process; authentic runtime verification is an
independent one-shot Windows action after strict safety preflights.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i11-two-owned-provider-no-model-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i11_owned_no_model_smoke_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_two_exact_independent_owned_Core_and_Bridge_instances() -> None:
    m = _module()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.PORTS == {"i11-a": 18881, "i11-b": 18882}
    assert m.TASKS == (
        ("bridge", "i11-a"),
        ("bridge", "i11-b"),
        ("core", "i11-a"),
        ("core", "i11-b"),
    )
    with pytest.raises(m.I11RealRuntimeRefusal):
        m.require(False, "refuse")


def test_result_mapping_and_scoped_disabled_gate() -> None:
    m = _module()
    assert m.data(
        SimpleNamespace(is_error=False, structured_content={"result": {"runtimes": []}})
    ) == {"runtimes": []}
    for item in (
        SimpleNamespace(is_error=True, structured_content={}),
        SimpleNamespace(is_error=False, structured_content=None, content=[]),
    ):
        with pytest.raises(m.I11RealRuntimeRefusal):
            m.data(item)
    assert m.is_disabled(
        {"registered": True, "enabled": False, "running": False, "state": "Disabled"}
    )
    assert not m.is_disabled(
        {"registered": False, "enabled": False, "running": False, "state": "Disabled"}
    )


def test_same_pid_requires_same_creation_time() -> None:
    m = _module()
    p = SimpleNamespace(pid=99, creation_time=1000)
    assert m.same(p, SimpleNamespace(pid=99, creation_time=1000))
    assert not m.same(p, SimpleNamespace(pid=99, creation_time=1001))
    assert not m.same(p, SimpleNamespace(pid=1, creation_time=1000))
    assert not m.same(None, p)


def test_only_four_owned_task_lifecycles_and_private_bearers() -> None:
    source = SOURCE.read_text("utf-8")
    for required in (
        'sys.argv[1:] == ["--run-once"]',
        "stage.postflight()",
        "stage.postflight()",
        "i4.protected_baseline(pre)",
        "load_native_core_bridge_binding(",
        '"envs/bridge/Scripts/python.exe"',
        "storage.check_private(auth, directory=False, sid=storage._sid())",
        "keys[IDS[0]] != keys[IDS[1]]",
        'started.append(("bridge", instance))',
        'started.append(("core", instance))',
        "for component, instance in reversed(started):",
        "I11_ALL_FOUR_SCOPED_TASKS_DISABLED=",
        "I11_TWO_INDEPENDENT_OWNED_CLAUDE_CORES_NATIVE_E2E=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        'session.call_tool("submit_agent_task"',
        'session.call_tool("run_repo_action"',
        "subprocess.Popen(",
        "shutil.rmtree(",
        "os.system(",
        "taskkill",
        "git push",
        "git commit",
        "print(keys)",
        "print(token)",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source, forbidden


def test_two_signed_owned_pipes_and_real_claude_discovery_with_cross_denial() -> None:
    source = SOURCE.read_text("utf-8")
    for required in (
        'binding.bridge_mode == "owned"',
        "binding.bridge_id == binding.instance_id == instance",
        "make_bridge_peer_validator(bridge_id=instance, user_sid=sid)",
        'signed.call("bridge.ping", {"workdir": "Fixture"})',
        'signed.call("runtime.list", {})',
        'item.get("name") == "claude"',
        'item.get("available") is True',
        "CoreGrantBridgeClient(",
        "key_id=b.key_id",
        "secret=b.secret",
        'exc.code == "CORE_NOT_AUTHORIZED"',
        "I11_FOREIGN_OWNED_BRIDGE_HMAC=DENIED",
    ):
        assert required in source, required


def test_actual_authenticated_http_mcp_and_peer_survival_scope() -> None:
    source = SOURCE.read_text("utf-8")
    parsed = ast.parse(source)
    funcs = {
        node.name
        for node in parsed.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"http_core", "both_cores", "signed_two_owned_bridges", "main"} <= funcs
    for required in (
        "cross.status_code == 401",
        "streamable_http_client(url, http_client=http)",
        "ClientSession(reader, writer) as session",
        'session.call_tool("list_workdirs", {})',
        'session.call_tool("list_agent_runtimes", {})',
        'fixture.get("access") == "read-write"',
        'fixture.get("agent_mode") == "workspace-write"',
        'fixture.get("repo_ops") is False',
        "await asyncio.gather(",
        'cli("bridge", IDS[0], "stop")',
        "_current_identity(bridge_a) is None",
        "I11_BRIDGE_B_AND_CORE_B_SURVIVE_BRIDGE_A_STOP=PASS",
    ):
        assert required in source, required
