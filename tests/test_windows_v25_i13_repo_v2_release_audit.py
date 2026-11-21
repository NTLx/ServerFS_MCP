"""Source-only tests for read-only I13 v2 native evidence acceptance."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i13-repo-v2-release-audit.py"
)


def module():
    spec = importlib.util.spec_from_file_location("i13_release_evidence", SOURCE)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def valid_capture(mod) -> str:
    # The fixed native result has separate pre/postflight markers around
    # service start, Repo actions, cleanup and terminal child exit.
    return (
        "\n".join([mod.START, *mod.BASELINE, *mod.REQUIRED[:-1], *mod.BASELINE, mod.REQUIRED[-1]])
        + "\n"
    )


def test_accepts_complete_unique_dual_repo_capture() -> None:
    mod = module()
    assert mod.validate(valid_capture(mod)) is None
    assert mod.EVIDENCE == "v0250-windows-i13-two-owned-repo-v2-capture-2026-11-17.txt"


@pytest.mark.parametrize(
    "damage", ["partial", "missing", "duplicate", "wrong_exit", "failed", "extra"]
)
def test_refuses_partial_or_conflicting_evidence(damage: str) -> None:
    mod = module()
    clean = valid_capture(mod)
    line = mod.REQUIRED[5]
    if damage == "partial":
        corrupt = mod.START + "\n"
    elif damage == "missing":
        corrupt = clean.replace(line + "\n", "")
    elif damage == "duplicate":
        corrupt = clean.replace(line + "\n", (line + "\n") * 2)
    elif damage == "wrong_exit":
        corrupt = clean.replace("I13_REPO_V2_CHILD_EXIT=0", "I13_REPO_V2_CHILD_EXIT=2")
    elif damage == "failed":
        corrupt = clean.replace(line, "I13_REPO_V2_OPERATION=FAILED")
    else:
        corrupt = clean + "I13_UNKNOWN=PASS\n"
    with pytest.raises(mod.I13EvidenceRefusal):
        mod.validate(corrupt)


def test_gate_is_readonly_and_never_replays_services() -> None:
    src = SOURCE.read_text("utf-8")
    for text in (
        'sys.argv[1:] == ["--audit"]',
        "current_user_sid() == storage._sid()",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "module.postflight()",
        "I13_REPO_V2_READONLY_RELEASE_EVIDENCE=PASS",
    ):
        assert text in src
    for forbidden in (
        "subprocess.run(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "run_repo_action",
        "submit_agent_task",
        "os.system(",
        "shell=True",
        "shutil.rmtree(",
    ):
        assert forbidden not in src
