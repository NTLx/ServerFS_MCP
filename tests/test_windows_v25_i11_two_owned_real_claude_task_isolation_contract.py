"""Source-only I11 genuine dual-Claude Agent and independent Bridge task isolation.

Simulated Core/Bridge API responses only; pytest never starts a Windows task,
reads tokens, starts Provider sessions, or uses any network connection.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp.agent_client import AgentBridgeRemoteError

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i11-two-owned-real-claude-task-isolation-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i11_two_real_claude_task_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_two_owned_core_instances_no_foreign_or_production_touches() -> None:
    m = _module()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.PORTS == {"i11-a": 18881, "i11-b": 18882}
    assert m.SENTINELS == {
        "i11-a": "I11_OWNED_REAL_A_OK",
        "i11-b": "I11_OWNED_REAL_B_OK",
    }
    assert m.TERMINAL == {"succeeded", "failed", "cancelled", "interrupted"}
    with pytest.raises(m.I11ProviderRefusal):
        m.require(False, "no")


def test_real_claude_task_is_submitted_once_per_core_only_via_http_mcp() -> None:
    source = SCRIPT.read_text("utf-8")
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"submit_one", "foreign_deny", "await_both", "real_pair", "main"} <= funcs.keys()
    submissions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "call_tool"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "submit_agent_task"
    ]
    assert len(submissions) == 1
    assert "return_exceptions=True" in source
    for needed in (
        '"runtime": "claude"',
        '"workdir": "Fixture"',
        '"profile": "workspace-write"',
        '"path": ""',
        "SENTINELS[instance]",
        "trust_env=False",
        "follow_redirects=False",
        "streamable_http_client(endpoint, http_client=http)",
        "ClientSession(reader, writer) as session",
        '"submit_agent_task"',
        "tasks[instance] = task",
        "I11_TWO_CONCURRENT_REAL_HTTP_MCP_TASK_SUBMISSIONS=PASS",
    ):
        assert needed in source, needed


def test_symmetric_foreign_task_methods_all_denied() -> None:
    m = _module()

    class DenyingBridge:
        def __init__(self):
            self.methods = []

        async def call(self, method, params):
            self.methods.append(method)
            raise AgentBridgeRemoteError("CORE_TASK_NOT_FOUND", "foreign-task-denied")

    async def check():
        pair = {"i11-a": DenyingBridge(), "i11-b": DenyingBridge()}
        await m.foreign_deny(pair, {"i11-a": "agt_AAAAA", "i11-b": "agt_BBBBB"})
        for bridge in pair.values():
            assert bridge.methods == ["task.get", "task.events", "task.result.read", "task.cancel"]
        with pytest.raises(m.I11ProviderRefusal):
            await m.foreign_deny(pair, {"i11-a": "agt_SAME", "i11-b": "agt_SAME"})

        class PermissiveBridge:
            async def call(self, method, params):
                return {}

        pair["i11-b"] = PermissiveBridge()
        with pytest.raises(m.I11ProviderRefusal):
            await m.foreign_deny(pair, {"i11-a": "agt_AAAAA", "i11-b": "agt_BBBBB"})

    asyncio.run(check())


def test_both_terminal_sentinels_and_content_free_event_audit() -> None:
    m = _module()

    class FakeOwnedBridge:
        def __init__(self, instance):
            self.instance = instance
            self.cursors = []

        async def call(self, method, params):
            if method == "task.get":
                return {"status": "succeeded", "final_response": m.SENTINELS[self.instance]}
            assert method == "task.events"
            self.cursors.append(params["after_event_id"])
            if params["after_event_id"] == 0:
                return {
                    "events": [{"event_type": "task.started"}],
                    "next_after_event_id": 1,
                }
            return {"events": [], "next_after_event_id": 1}

    def safe_audit(events):
        assert all(item["event_type"] != "tool.started" for item in events)
        return len(events)

    async def check():
        clients = {name: FakeOwnedBridge(name) for name in m.IDS}
        completed = {}
        await m.await_both(
            clients,
            {name: "agt-" + name for name in m.IDS},
            completed,
            SimpleNamespace(assert_no_unsafe_event=safe_audit),
        )
        assert completed == {"i11-a": True, "i11-b": True}
        assert clients["i11-a"].cursors == [0, 1]
        assert clients["i11-b"].cursors == [0, 1]

        class WrongAnswer(FakeOwnedBridge):
            async def call(self, method, params):
                if method == "task.get":
                    return {"status": "succeeded", "final_response": "WRONG"}
                return await super().call(method, params)

        clients["i11-b"] = WrongAnswer("i11-b")
        with pytest.raises(m.I11ProviderRefusal):
            await m.await_both(
                clients,
                {name: "agt-" + name for name in m.IDS},
                {},
                SimpleNamespace(assert_no_unsafe_event=safe_audit),
            )

    asyncio.run(check())


def test_exact_scope_scoped_cleanup_and_protected_owner_generation() -> None:
    source = SCRIPT.read_text("utf-8")
    for required in (
        'sys.argv[1:] == ["--run-once"]',
        "i11.postflight()",
        "i4.protected_baseline(gate)",
        "workdirs[0].root.resolve(strict=True) == fixture.resolve(strict=True)",
        'workdirs[0].agent_runtimes == frozenset({"claude"})',
        "not workdirs[0].policy.repo_ops_enabled",
        "keys[IDS[0]] != keys[IDS[1]]",
        'binding.bridge_mode == "owned"',
        "make_bridge_peer_validator(bridge_id=instance, user_sid=storage._sid())",
        'started.append(("bridge", instance))',
        'started.append(("core", instance))',
        "for component, instance in reversed(started):",
        "clients[instance].call(",
        '"task.cancel"',
        "runtime.same(generation, current)",
        "I11_REAL_PROVIDER_FOUR_OWNERS_DISABLED=",
        "I11_REAL_PROVIDER_PRODUCTION_I4_I7=UNCHANGED",
        "I11_TWO_INDEPENDENT_REAL_CLAUDE_OWNERS_AND_TASK_ISOLATION=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        'session.call_tool("run_repo_action"',
        'session.call_tool("create_text_file"',
        "subprocess.Popen(",
        "shutil.rmtree(",
        "os.system(",
        "taskkill",
        "git commit",
        "git push",
        "print(task_id",
        "print(tasks",
        "print(keys",
        "print(token",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in source, forbidden
