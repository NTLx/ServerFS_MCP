"""Source guard for the opt-in named Connector KeepAlive acceptance path."""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/macos/v0250_named_connector_lifecycle_acceptance.py"
)
_DEPLOYMENT = _SCRIPT.parent
if str(_DEPLOYMENT) not in sys.path:
    sys.path.insert(0, str(_DEPLOYMENT))
_SPEC = importlib.util.spec_from_file_location("named_connector_acceptance", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_ACCEPTANCE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ACCEPTANCE)


def test_keepalive_authenticated_mcp_roundtrips_are_awaited_around_sigkill() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    keepalive = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "args"
        and node.test.attr == "verify_keepalive"
    )
    calls = sorted(
        (node.lineno, node)
        for node in ast.walk(keepalive)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_mcp_roundtrip"
    )
    assert len(calls) == 2

    parents: dict[ast.AST, ast.AST] = {
        child: parent for parent in ast.walk(keepalive) for child in ast.iter_child_nodes(parent)
    }
    for _, call in calls:
        wrapper = parents[call]
        assert isinstance(wrapper, ast.Call)
        assert isinstance(wrapper.func, ast.Attribute)
        assert isinstance(wrapper.func.value, ast.Name)
        assert wrapper.func.value.id == "asyncio"
        assert wrapper.func.attr == "run"
    kill_line = next(
        node.lineno
        for node in ast.walk(keepalive)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "kill"
    )
    assert calls[0][0] < kill_line < calls[1][0]


def test_verified_sleep_pid_requires_exact_pid_uid_argv_and_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = tmp_path / "synthetic-cloudflared"
    fixture = "#!/bin/sh\nexec /bin/sleep 300\n"
    dummy.write_text(fixture, encoding="utf-8")
    observed = ["4242", str(_ACCEPTANCE.os.getuid()), "/bin/sleep 300"]

    class Result:
        returncode = 0
        stdout = " ".join(observed)

    monkeypatch.setattr(_ACCEPTANCE.subprocess, "run", lambda *args, **kwargs: Result())
    assert _ACCEPTANCE._verified_sleep_pid(4242, dummy)

    for row in (
        f"4243 {observed[1]} /bin/sleep 300",
        f"4242 {observed[1]} /bin/sleep 301",
        f"4242 {observed[1]} /bin/sleep 300 --extra",
        f"4242 {int(observed[1]) + 1} /bin/sleep 300",
    ):
        monkeypatch.setattr(
            _ACCEPTANCE.subprocess,
            "run",
            lambda *args, row=row, **kwargs: type("Result", (), {"returncode": 0, "stdout": row})(),
        )
        assert not _ACCEPTANCE._verified_sleep_pid(4242, dummy)
    monkeypatch.setattr(
        _ACCEPTANCE.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )
    dummy.write_text("#!/bin/sh\nexec /bin/sleep 301\n", encoding="utf-8")
    assert not _ACCEPTANCE._verified_sleep_pid(4242, dummy)


def test_keepalive_flag_requires_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--verify-keepalive"])
    with pytest.raises(SystemExit) as exc:
        _ACCEPTANCE.main()
    assert exc.value.code == 2
