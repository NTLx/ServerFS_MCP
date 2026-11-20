"""I13: SOURCE ONLY Windows preflight contract for two new Repo-only owned pairs."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-owned-repo-preflight.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_repo_preflight_contract", SCRIPT)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_two_exact_unused_names_and_ports() -> None:
    mod = module()
    assert mod.IDS == ("i13-a", "i13-b")
    assert mod.PORTS == {"i13-a": 18883, "i13-b": 18884}
    assert mod.TASK_NAMES == {
        "ServerFS v25 i13-a Core",
        "ServerFS v25 i13-a Agent Bridge",
        "ServerFS v25 i13-b Core",
        "ServerFS v25 i13-b Agent Bridge",
    }


def test_new_tasks_must_be_absent() -> None:
    mod = module()
    mod.verify_tasks([], installed=False)
    with pytest.raises(mod.I13PreflightRefusal):
        mod.verify_tasks([{"name": "other"}], installed=False)


def test_postflight_requires_four_exact_disabled_tasks() -> None:
    mod = module()
    exact = [
        {"name": name, "state": "Disabled", "enabled": False} for name in sorted(mod.TASK_NAMES)
    ]
    mod.verify_tasks(exact, installed=True)
    for changed in (
        exact[:3],
        [{**item, "state": "Ready"} for item in exact],
        [{**item, "enabled": True} for item in exact],
    ):
        with pytest.raises(mod.I13PreflightRefusal):
            mod.verify_tasks(changed, installed=True)


def test_source_guards_protected_installed_and_avoids_native_mutation() -> None:
    source = SCRIPT.read_text("utf-8")
    for required in (
        "old.postflight()",
        "verify_tasks(task_rows(), installed=False)",
        "not root.exists() and not root.is_symlink()",
        "not any(item.port in PORTS.values() for item in listeners)",
        'shutil.which("git")',
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "current_user_sid() == storage._sid()",
        "I13_TWO_REPO_ONLY_OWNED_PAIRS_PREFLIGHT=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "subprocess.Popen(",
        "shutil.rmtree(",
        "cloudflare.request(",
        "git init",
        "git commit",
        "git push",
        "os.system(",
        "shell=True",
    ):
        assert forbidden not in source, forbidden
