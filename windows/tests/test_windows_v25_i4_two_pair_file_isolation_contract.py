"""Source-only safety contracts for installed topology C real file isolation.

Never starts Scheduler/Provider/Tunnel, writes host files or reads tokens.
Native installed I4 --run-once is an independent explicit E2E gate.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i4-two-pair-file-isolation-smoke.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i4_file_isolation_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_only_preinstalled_two_exact_owned_windows_pairs_used() -> None:
    mod = _module()
    assert mod.IDS == ("i4-a", "i4-b")
    assert mod.PORTS == {"i4-a": 18871, "i4-b": 18872}


def test_fail_closed_tool_result_mapping_is_sealed_and_supports_real_sdk() -> None:
    m = _module()
    assert m.tool_data(
        SimpleNamespace(is_error=False, structured_content={"result": {"content": "ok"}})
    ) == {"content": "ok"}
    assert m.tool_data(SimpleNamespace(is_error=False, structured_content={"content": "ok"})) == {
        "content": "ok"
    }
    assert m.tool_data(
        SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[SimpleNamespace(type="text", text='{"result":{"content":"ok"}}')],
        )
    ) == {"content": "ok"}
    for item in (
        SimpleNamespace(is_error=True, structured_content={"result": {"content": "bad"}}),
        SimpleNamespace(is_error=False, structured_content=None, content=[]),
        SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[SimpleNamespace(type="text", text="not-json")],
        ),
    ):
        with pytest.raises(m.I4FileIsolationRefusal):
            m.tool_data(item)


def test_exact_scoped_cli_and_no_runtime_or_repo_mutations() -> None:
    body = SOURCE.read_text("utf-8")
    for expected in (
        'sys.argv[1:] == ["--run-once"]',
        '"envs/bridge/Scripts/python.exe"',
        "smoke.four_disabled(gate)",
        "smoke.protected_baseline(gate)",
        'storage.root(instance) / "workdirs/fixture"',
        "storage.check_private(fixture, directory=True, sid=storage._sid())",
        "workdirs[0].root",
        "workdirs[0].read_only is True",
        'installer._new_private_file(path, content[instance].encode("ascii"), storage._sid())',
        "storage.check_private(path, directory=False, sid=storage._sid())",
        "await session.call_tool(",
        '"read_text_file"',
        '"create_text_file"',
        '"list_agent_runtimes"',
        'runtimes.get("runtimes") == []',
        "wrong.status_code == 401",
        'getattr(denied, "is_error", getattr(denied, "isError", False)) is True',
        "await smoke.wrong_bearer_is_refused(PORTS[instance], own_key)",
        'started.append(("bridge", instance))',
        'started.append(("core", instance))',
        "for component, instance in reversed(started):",
        "path.unlink()",
        "I4_TWO_OWNED_PAIRS_REAL_FILE_ISOLATION_E2E=PASS",
    ):
        assert expected in body, expected
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "shutil.rmtree(",
        "os.system(",
        "taskkill",
        "git commit",
        "git push",
        'session.call_tool("run_repo_action"',
        'session.call_tool("submit_agent_task"',
        "ANTHROPIC_API_KEY",
        "print(keys",
        "print(auth",
        "shell=True",
    ):
        assert forbidden not in body, forbidden


def test_distinct_source_contents_and_safe_bounded_deletion() -> None:
    body = SOURCE.read_text("utf-8")
    assert 'IDS[0]: "I4_C_NATIVE_READ_A\\n"' in body
    assert 'IDS[1]: "I4_C_NATIVE_READ_B\\n"' in body
    assert 'name = "i4-owned-files-" + secrets.token_hex(10) + ".txt"' in body
    assert "for path in reversed(created):" in body
    assert "path.name == name" in body
    assert "path.parent in fixtures.values()" in body
    assert "path.stat().st_size <= 128" in body
    assert "path.unlink()" in body
    assert "and not (path / name).is_symlink()" in body
    assert "I4_FILE_ALL_FOUR_TASKS_AND_MARKERS_RESTORED=" in body


def test_two_concurrent_mcp_sessions_and_cross_core_stop_survival() -> None:
    src = SOURCE.read_text("utf-8")
    parsed = ast.parse(src)
    names = {
        node.name
        for node in parsed.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"one_core", "both", "main", "tool_data", "is_disabled"} <= names
    assert "await asyncio.gather(" in src
    assert "one_core(IDS[0], keys[IDS[0]], keys[IDS[1]], filename, text[IDS[0]])" in src
    assert "one_core(IDS[1], keys[IDS[1]], keys[IDS[0]], filename, text[IDS[1]])" in src
    assert 'smoke.cli("core", IDS[0], "stop")' in src
    assert "I4_FILE_CORE_B_SURVIVES_CORE_A_STOP=PASS" in src


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"registered": True, "enabled": False, "running": False, "state": "Disabled"}, True),
        ({"registered": True, "enabled": True, "running": False, "state": "Ready"}, False),
        ({"registered": True, "enabled": False, "running": True, "state": "Running"}, False),
        ({}, False),
    ],
)
def test_scoped_stop_requires_exact_disabled_status(status: dict, expected: bool) -> None:
    assert _module().is_disabled(status) is expected
