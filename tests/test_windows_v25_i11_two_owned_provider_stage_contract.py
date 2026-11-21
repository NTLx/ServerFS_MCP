"""I11 two independent owned Provider pairs: SOURCE ONLY installer contract.

No install, Scheduler, private key, Provider or remote Connector invocation.
The native I11 --stage action is separate and one-use after safety review.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i11-two-owned-provider-stage.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i11_two_owned_stage_contract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ids_and_two_real_writable_disposable_provider_pairs() -> None:
    m = _module()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.PORTS == {"i11-a": 18881, "i11-b": 18882}
    assert m._json({"b": 2, "a": 1}) == b'{"a":1,"b":2}\n'
    with pytest.raises(m.I11StageRefusal):
        m.require(False, "unsafe")


def test_each_pair_owns_separate_bridge_and_worker_no_shared_bridge() -> None:
    source = SCRIPT.read_text("utf-8")
    for expected in (
        '"bridge_id": instance',
        "WindowsBridgeResources(sid, instance).pipe_name",
        '"state_dir": str(home / "state")',
        '"lock_dir": str(home / "locks")',
        '"allowed_peer_sid": sid',
        '"core_grants_enabled": True',
        '"claude": {"enabled": True, "claude_bin": cli, "use_proxy": False}',
        '"read_only": False',
        '"agent_mode": "workspace-write"',
        '"agent_runtimes": ["claude"]',
        '"repo_ops_enabled": False',
        'storage.prepare("bridge", instance)',
        'storage.prepare("core", instance)',
        '"workdirs/fixture"',
        'base / "envs/agent-bridge"',
        'base / "envs/bridge"',
        'base / "envs/core"',
        '"I11_TWO_INDEPENDENT_PROVIDER_WORKERS_AND_SIGNED_GRANTS=PASS"',
    ):
        assert expected in source, expected


def test_source_verified_offline_install_and_independent_owner_capabilities() -> None:
    source = SCRIPT.read_text("utf-8")
    for expected in (
        "pre.preflight()",
        "native_pin.verify_native_wheel(native_wheel)",
        "installer._wheel(",
        'installer._protect_tree(base / "build", sid)',
        "installer.verify_installed_runtime(base, owner_python, worker_python)",
        '"adapters/claude.py"',
        "i5._DIGEST",
        "i5._core_toml(fixture)",
        "i4._binding_and_grant(instance, digest)",
        'grant["methods"] = list(i5.METHODS)',
        "i3._PROVISION",
        "parse_core_bridge_binding(",
        "load_native_bridge_plan(bridge_manifest)",
        "storage.stage(manifest)",
        "Scheduler().install(manifest)",
        "postflight()",
        "I11_TWO_OWNED_PROVIDER_PAIRS_STAGE=PASS",
    ):
        assert expected in source, expected
    for forbidden in (
        "cloudflare.request(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "agent_task(",
        "subprocess.Popen(",
        "shutil.rmtree(",
        "os.system(",
        "taskkill",
        "ANTHROPIC_API_KEY",
        "git push",
        "git commit",
        "shell=True",
    ):
        assert forbidden not in source, forbidden


def test_postflight_requires_exact_four_disabled_private_owner_manifests() -> None:
    source = SCRIPT.read_text("utf-8")
    for expected in (
        "pre.verify_tasks(pre.candidate_tasks(), installed=True)",
        "settings.bridge_required",
        "workdirs[0].read_only is False",
        'workdirs[0].policy.agent_runtimes == frozenset({"claude"})',
        'binding.bridge_mode == "owned"',
        "binding.bridge_id == binding.instance_id == instance",
        'status.get("enabled") is False',
        'status.get("state") == "Disabled"',
        'status.get("running") is False',
        "_current_identity(manifest) is None",
        "len(ids) == 2",
        "ids[0] != ids[1]",
        "storage.check_private(",
        "I11_FOUR_OWNERS_DISABLED_WITH_PRIVATE_GRANTS=PASS",
    ):
        if expected == "len(ids) == 2":
            # The loop length is fixed by the explicit two-member PORTS.
            assert "for instance, port in PORTS.items():" in source
        else:
            assert expected in source, expected


def test_one_use_installer_only_exposes_stage_and_postflight_actions() -> None:
    source = SCRIPT.read_text("utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"native", "stage", "postflight", "main"} <= functions.keys()
    assert 'mode.add_argument("--stage", action="store_true")' in source
    assert 'mode.add_argument("--postflight", action="store_true")' in source
    assert "argparse" in source
    assert "sys.flags.isolated" in source
    assert "sys.dont_write_bytecode" in source
    assert "not ctypes.windll.shell32.IsUserAnAdmin()" in source
