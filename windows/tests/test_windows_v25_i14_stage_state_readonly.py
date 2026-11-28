"""I14 deterministic read-only post-stage snapshot, no native process required."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "deployment/windows/v0250-i14-stage-state-readonly.py"
STAGE = ROOT / "deployment/windows/v0250-i14-two-owned-repo-public-connector-stage.py"


def load():
    spec = importlib.util.spec_from_file_location("i14_readonly_snapshot_contract", SNAPSHOT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(names):
    return [{"name": name, "enabled": False, "state": "Disabled"} for name in sorted(names)]


def fixture():
    mod = load()
    cores = frozenset(
        {
            "ServerFS v25 i13-a Core",
            "ServerFS v25 i13-b Core",
            "ServerFS v25 i13-a Agent Bridge",
            "ServerFS v25 i13-b Agent Bridge",
        }
    )
    connectors = frozenset(
        {
            "ServerFS v25 i13-a Cloudflare Connector i14-edge-a",
            "ServerFS v25 i13-b Cloudflare Connector i14-edge-b",
        }
    )
    return mod, cores, connectors


def test_fresh_two_connector_slots_are_absent() -> None:
    mod, cores, connectors = fixture()
    assert (
        mod.classify(rows(cores), core_names=cores, connectors=connectors, exists=(False,) * 16)
        == "FRESH"
    )


def test_completed_layout_is_only_candidate_not_historical_exit_proof() -> None:
    mod, cores, connectors = fixture()
    assert (
        mod.classify(
            rows(cores | connectors),
            core_names=cores,
            connectors=connectors,
            exists=(True,) * 16,
        )
        == "COMPLETE_LAYOUT"
    )
    source = SNAPSHOT.read_text("utf-8")
    assert "it does not attest historical stage process exit" in source
    assert "stage.postflight()" not in source
    assert "Scheduler().install(" not in source


@pytest.mark.parametrize("missing", [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 14, 15])
def test_partial_install_is_never_accepted_as_complete(missing: int) -> None:
    mod, cores, connectors = fixture()
    exists = tuple(i != missing for i in range(16))
    assert (
        mod.classify(
            rows(cores | connectors), core_names=cores, connectors=connectors, exists=exists
        )
        == "PARTIAL_OR_IN_PROGRESS"
    )


@pytest.mark.parametrize("missing_runtime", [(4, 5, 12, 13), (4, 5), (12, 13)])
def test_disabled_only_install_does_not_require_ephemeral_runtime_markers(
    missing_runtime: tuple[int, ...],
) -> None:
    mod, cores, connectors = fixture()
    presence = tuple(i not in missing_runtime for i in range(16))
    assert (
        mod.classify(
            rows(cores | connectors),
            core_names=cores,
            connectors=connectors,
            exists=presence,
        )
        == "COMPLETE_LAYOUT"
    )


def test_task_created_without_files_and_files_without_tasks_are_partial() -> None:
    mod, cores, connectors = fixture()
    for selected, presence in (
        (cores, (True,) * 16),
        (cores | connectors, (False,) * 16),
        (cores | {next(iter(connectors))}, (True,) * 16),
    ):
        assert (
            mod.classify(rows(selected), core_names=cores, connectors=connectors, exists=presence)
            == "PARTIAL_OR_IN_PROGRESS"
        )


def test_fails_closed_on_unknown_duplicate_or_enabled_task() -> None:
    mod, cores, connectors = fixture()
    good = rows(cores | connectors)
    for bad in (
        good + [good[0]],
        good + [{"name": "ServerFS v25 i13-unrelated Core", "enabled": False, "state": "Disabled"}],
        [{**x, "enabled": True} for x in good],
        [{**x, "state": "Running"} for x in good],
        good[:2],
    ):
        with pytest.raises(mod.I14StateRefusal):
            mod.classify(bad, core_names=cores, connectors=connectors, exists=(True,) * 16)


@pytest.mark.parametrize("bad", [(True,) * 15, (True,) * 17, (1,) * 16])
def test_rejects_corrupted_snapshot_shape(bad) -> None:
    mod, cores, connectors = fixture()
    with pytest.raises(mod.I14StateRefusal):
        mod.classify(rows(cores), core_names=cores, connectors=connectors, exists=bad)


def test_details_only_discloses_bounded_presence_flags() -> None:
    mod = load()
    assert mod.FIELDS == (
        "HOME",
        "MANIFEST",
        "TASK_XML",
        "TOKEN_PRESENT",
        "IDENTITY",
        "RUNTIME_LOG",
        "PYTHON",
        "PYTHONW",
    )
    source = SNAPSHOT.read_text("utf-8")
    assert 'parser.add_argument("--details", action="store_true")' in source
    assert "snapshot(details=args.details)" in source
    assert '("PRESENT" if present else "ABSENT")' in source
    assert "item.read_bytes()" not in source
    assert "print(str(item))" not in source


def test_source_reuses_native_task_preflight_and_never_reads_token() -> None:
    source = SNAPSHOT.read_text("utf-8")
    for expected in (
        "pre.task_rows()",
        "pre.TASK_NAMES",
        "pre.OPTIONAL_CONNECTOR_NAMES",
        "item.is_symlink()",
        "item.exists()",
        "I14_READONLY_LAYOUT_STATE=",
    ):
        assert expected in source
    for forbidden in (
        "read_bytes(",
        "read_text(",
        "subprocess.Popen(",
        "Scheduler().start(",
        "Scheduler().install(",
        "Scheduler().stop(",
        "Scheduler().uninstall(",
        "cloudflare.request(",
        "os.system(",
    ):
        assert forbidden not in source
    assert STAGE.is_file()
