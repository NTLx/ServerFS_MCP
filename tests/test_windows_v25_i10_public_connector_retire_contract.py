"""I10 exact nonproduction Connector retirement contracts (pure source test).

Never touches real Cloudflare Tunnel, Scheduler, tokens or private file homes.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i10-public-connector-retire.py"
)


def _source():
    spec = importlib.util.spec_from_file_location("i10_retire_contract", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_i10_only_retirement_ids_and_evidence_inventory() -> None:
    m = _source()
    assert m.CORE == "i5-core-a"
    assert m.IDS == ("i10-edge-a", "i10-edge-b")
    assert m.OWNED == {"service.json", "task.xml", "tunnel.token"}
    assert m.EVIDENCE == {"identity.json", "runtime.log"}
    m.exact_names(set(m.OWNED | m.EVIDENCE), retired=False)
    m.exact_names(set(m.EVIDENCE), retired=True)
    for altered in (set(), set(m.OWNED), set(m.EVIDENCE | {"foreign.json"})):
        with pytest.raises(m.I10RetireRefusal):
            m.exact_names(altered, retired=True)


def test_disabled_only_strict_scope_and_remote_restore_acknowledgement() -> None:
    src = SCRIPT.read_text("utf-8")
    for expected in (
        '"--retire-after-remote-restore"',
        "stage.postflight()",
        "_current_identity(manifest) is None",
        'status.get("state") == "Disabled"',
        'status.get("enabled") is False',
        'status.get("running") is False',
        "scheduler.uninstall(manifest)",
        "storage.check_private(path, directory=False, sid=storage._sid())",
        "path.unlink()",
        "old.postflight()",
        "i7.postflight()",
        "i5.verify_candidates(candidates, installed=True)",
        'set(row["name"] for row in candidates) == set(i5.TARGETS)',
        "I10_ONLY_NATIVE_RUNTIME_EVIDENCE_RETAINED=PASS",
    ):
        assert expected in src, expected
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "subprocess.run(",
        "cloudflare.request(",
        "shutil.rmtree(",
        "os.system(",
        "taskkill",
        "print(token)",
        "git push",
    ):
        assert forbidden not in src


def test_exact_retirement_functions_and_no_unbounded_delete() -> None:
    src = SCRIPT.read_text("utf-8")
    tree = ast.parse(src)
    methods = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"native", "audit", "retire", "postflight", "main"} <= methods.keys()
    routine = ast.get_source_segment(src, methods["retire"])
    assert routine is not None
    assert "manifests = audit()" in routine
    assert "for manifest in reversed(manifests):" in routine
    assert 'for name in ("tunnel.token", "task.xml", "service.json"):' in routine
    assert "exact_names(" in routine
    assert routine.index("scheduler.uninstall") < routine.index("path.unlink()")


def test_no_tokens_leaked_from_refusal_channel() -> None:
    m = _source()
    m.require(True, "ok")
    with pytest.raises(m.I10RetireRefusal):
        m.require(False, "stop")
