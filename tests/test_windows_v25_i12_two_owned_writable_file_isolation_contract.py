"""I12 safe source-only checks for two independent Windows native writable pairs.

No private files, Windows Task Scheduler, localhost HTTP, model/Provider,
Cloudflare or installed I11 instance is accessed by this pytest module.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i12-two-owned-writable-file-isolation-smoke.py"
)


def script():
    spec = importlib.util.spec_from_file_location("i12_two_owned_writes_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_exact_two_disjoint_i11_core_instances() -> None:
    m = script()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.PORTS == {"i11-a": 18881, "i11-b": 18882}
    assert m.START == {
        "i11-a": "I12_OWNED_A_FIRST\n",
        "i11-b": "I12_OWNED_B_FIRST\n",
    }
    assert m.EDITED["i11-a"] != m.EDITED["i11-b"]
    assert m.SURVIVOR != m.EDITED["i11-b"]


def test_mcp_reply_failure_is_explicit() -> None:
    m = script()
    assert m.errored(SimpleNamespace(is_error=True))
    assert m.errored(SimpleNamespace(isError=True))
    assert not m.errored(SimpleNamespace(is_error=False))
    assert not m.errored(SimpleNamespace())


@pytest.mark.parametrize(
    "content",
    [
        "I12_OWNED_A_FIRST\n",
        "I12_OWNED_B_FIRST\n",
        "I12_OWNED_A_EDITED\n",
        "I12_OWNED_B_EDITED\n",
        "I12_OWNED_B_AFTER_A_STOP\n",
    ],
)
def test_bounded_file_cleanup_only_known_test_bytes(tmp_path, content) -> None:
    m = script()
    name = "i12-owned-write-known.txt"
    probe = tmp_path / name
    probe.write_bytes(content.encode("ascii"))
    assert m.bounded_fixture_file(probe, name=name, root=tmp_path)
    assert not probe.exists()


def test_bounded_cleanup_must_preserve_unknown_file(tmp_path) -> None:
    m = script()
    name = "i12-owned-write-known.txt"
    probe = tmp_path / name
    probe.write_text("UNKNOWN PRIVATE USER FILE\n", encoding="ascii")
    with pytest.raises(m.I12WriteRefusal):
        m.bounded_fixture_file(probe, name=name, root=tmp_path)
    assert probe.read_text("ascii") == "UNKNOWN PRIVATE USER FILE\n"


def test_bounded_cleanup_rejects_wrong_parent_or_basename(tmp_path) -> None:
    m = script()
    path = tmp_path / "safe.txt"
    path.write_text(m.START["i11-a"], encoding="ascii")
    with pytest.raises(m.I12WriteRefusal):
        m.bounded_fixture_file(path, name="other.txt", root=tmp_path)
    with pytest.raises(m.I12WriteRefusal):
        m.bounded_fixture_file(path, name="safe.txt", root=tmp_path / "other")
    assert path.exists()


def test_bounded_cleanup_rejects_large_file(tmp_path) -> None:
    m = script()
    probe = tmp_path / "large.txt"
    probe.write_text("x" * 130, encoding="ascii")
    with pytest.raises(m.I12WriteRefusal):
        m.bounded_fixture_file(probe, name=probe.name, root=tmp_path)
    assert probe.exists()


def test_concurrent_writes_dispatch_exactly_both_owners(monkeypatch) -> None:
    m = script()
    seen = []

    async def fake_file_core(instance, token, other, filename, attempted):
        seen.append((instance, token, other, filename))
        attempted.add(instance)

    monkeypatch.setattr(m, "file_core", fake_file_core)
    attempted = set()
    asyncio.run(m.concurrent_writes({"i11-a": "aaa", "i11-b": "bbb"}, "private.txt", attempted))
    assert set(attempted) == {"i11-a", "i11-b"}
    assert sorted(seen) == [
        ("i11-a", "aaa", "bbb", "private.txt"),
        ("i11-b", "bbb", "aaa", "private.txt"),
    ]


def test_real_http_file_mutation_only_under_private_scoped_fixture() -> None:
    body = SOURCE.read_text("utf-8")
    tree = ast.parse(body)
    for required in (
        "httpx2.AsyncClient(",
        "trust_env=False",
        "follow_redirects=False",
        "streamable_http_client(url, http_client=http)",
        "ClientSession(reader, writer) as session",
        "rejected.status_code == 401",
        '"workdir": "Fixture"',
        '"create_text_file"',
        '"read_text_file"',
        '"edit_text_file"',
        "attempted.add(instance)",
        'created.get("created") is True',
        "first_revision",
        "errored(duplicate)",
        "errored(stale)",
        'changed.get("edits_applied") == 1',
        'before.get("content") == EDITED[IDS[1]]',
        'updated.get("content") == SURVIVOR',
    ):
        assert required in body, required
    function_names = {
        item.name for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "file_core",
        "b_survives_and_writes",
        "concurrent_writes",
        "bounded_fixture_file",
        "main",
    } <= function_names


def test_real_native_task_lifecycle_cleanup_and_production_guard() -> None:
    body = SOURCE.read_text("utf-8")
    for required in (
        'sys.argv[1:] == ["--run-once"]',
        "stage.postflight()",
        "i4.protected_baseline(gate)",
        "storage.check_private(root, directory=True, sid=storage._sid())",
        "workdirs[0].root.resolve(strict=True) == root.resolve(strict=True)",
        "workdirs[0].read_only is False",
        "workdirs[0].policy.repo_ops_enabled is False",
        "keys[IDS[0]] != keys[IDS[1]]",
        "secrets.token_hex(12)",
        "not (directory / filename).exists()",
        'started.append(("bridge", instance))',
        'started.append(("core", instance))',
        'runtime.cli("core", IDS[0], "stop")',
        "for component, instance in reversed(started):",
        "for instance in IDS:",
        "if instance not in attempted:",
        "bounded_fixture_file(",
        "runtime.same(generation, _current_identity(prod))",
        "I12_EXACT_FOUR_I11_TASKS_AND_TWO_PROBES_CLEANED=",
        "I12_TWO_INDEPENDENT_OWNED_WRITABLE_CORES_NATIVE_E2E=PASS",
    ):
        assert required in body, required
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
        "git commit",
        "git push",
        "print(keys",
        "print(auth",
        "ANTHROPIC_API_KEY",
    ):
        assert forbidden not in body, forbidden
