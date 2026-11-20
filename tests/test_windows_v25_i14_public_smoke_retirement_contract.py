"""I14 source contracts for owned two-Core public Repo Ops and scoped retirement.

No Cloudflare calls, native service/task registration, HTTP or file writes.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "deployment/windows/v0250-i14-two-owned-repo-public-connector-stage.py"
SMOKE = ROOT / "deployment/windows/v0250-i14-two-owned-repo-public-smoke.py"
RETIRE = ROOT / "deployment/windows/v0250-i14-two-owned-repo-public-connector-retire.py"


def load(path: Path, label: str):
    spec = importlib.util.spec_from_file_location(label, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_route_is_exactly_same_installed_two_cores() -> None:
    stage = load(STAGE, "i14_public_source_stage")
    smoke = load(SMOKE, "i14_public_source_smoke")
    assert tuple((c, con, port) for c, con, _old, port, _host in stage.ROUTES) == tuple(
        (c, con, port) for c, con, _host, port in smoke.ROUTES
    )
    assert smoke.ROUTES[0][2] == "workpc-v25a.ntlx.top"
    assert smoke.ROUTES[1][2] == "workpc-v25b.ntlx.top"


def test_public_smoke_really_checks_auth_repo_and_peer_survival() -> None:
    source = SMOKE.read_text("utf-8")
    for expected in (
        'url = "https://" + host + "/mcp"',
        '"Authorization": "Bearer " + foreign',
        "missing.status_code == wrong.status_code == cross.status_code == 401",
        '"Host": "invalid.test"',
        '"Origin": "https://invalid.test"',
        '"list_workdirs", "inspect_repo", "run_repo_action"',
        '"submit_agent_task" not in names',
        '"list_agent_runtimes" not in names',
        '"view": "actions"',
        '"action": "status_probe"',
        'getattr(unknown, "is_error"',
        'binding.bridge_mode == "owned"',
        "scheduler.start(item)",
        'scheduler.stop(manifests[("cloudflare", ROUTES[0][0])])',
        'scheduler.stop(manifests[("bridge", ROUTES[0][0])])',
        "prior.protected_baseline(gate) == baseline",
        "I14_TWO_INDEPENDENT_OWNED_REPO_PUBLIC_E2E=PASS",
    ):
        assert expected in source, expected
    for forbidden in (
        "cloudflare.request(",
        "requests.patch(",
        "os.system(",
        "shell=True",
        "git push",
        "git commit",
        "ANTHROPIC_API_KEY",
        "print(keys)",
    ):
        assert forbidden not in source, forbidden


def test_public_smoke_has_six_only_scoped_stops_and_final_cleanup() -> None:
    tree = ast.parse(SMOKE.read_text("utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    body = ast.get_source_segment(SMOKE.read_text("utf-8"), functions["main"])
    assert body is not None
    assert body.index("stage.postflight()") < body.index("scheduler.start(item)")
    assert body.index("finally:") < body.index("stage.postflight()", body.index("finally:"))
    assert body.count("scheduler.start(item)") == 1
    assert body.count("scheduler.stop(") == 3
    assert "for item in reversed(attempted)" in body


def test_retirement_exact_manifest_and_token_scope() -> None:
    retire = load(RETIRE, "i14_public_retire_scope")
    assert retire.ROUTES == (("i13-a", "i14-edge-a"), ("i13-b", "i14-edge-b"))
    assert retire.OWNED == {"service.json", "task.xml", "tunnel.token"}
    assert retire.EVIDENCE == {"identity.json", "runtime.log"}
    retire.inventory(set(retire.OWNED | retire.EVIDENCE), retired=False)
    retire.inventory(set(retire.EVIDENCE), retired=True)
    for invalid, retired in (
        (set(retire.OWNED), False),
        (set(retire.EVIDENCE) | {"tunnel.token"}, True),
        (set(retire.EVIDENCE) | {"sensitive.jwt"}, True),
    ):
        with pytest.raises(retire.I14RetireRefusal):
            retire.inventory(invalid, retired=retired)


def test_retirement_only_uninstalls_explicit_disabled_native_owners() -> None:
    source = RETIRE.read_text("utf-8")
    for expected in (
        "stage.postflight()",
        "scheduler.uninstall(manifest)",
        'for name in ("tunnel.token", "task.xml", "service.json"):',
        "storage.check_private(file, directory=False, sid=storage._sid())",
        "inventory({item.name for item in home.iterdir()}, retired=True)",
        "--retire-after-verified-remote-restore",
        "stage.baseline()",
        "I14_TEMPORARY_TWO_CONNECTORS_RETIRED=PASS",
    ):
        assert expected in source, expected
    for forbidden in (
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "subprocess.Popen(",
        "shutil.rmtree(",
        "git push",
        "print(token)",
        "shell=True",
    ):
        assert forbidden not in source, forbidden


def test_refusals_are_bounded() -> None:
    smoke = load(SMOKE, "i14_public_refusal")
    retire = load(RETIRE, "i14_retire_refusal")
    with pytest.raises(smoke.I14PublicRefusal):
        smoke.require(False, "stop")
    with pytest.raises(retire.I14RetireRefusal):
        retire.require(False, "stop")
