"""Source-only guardrails for temporary Windows I5 Connector deprovision."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1] / "deployment/windows/v0250-i5-public-connector-retire.py"
)
spec = importlib.util.spec_from_file_location("i5_conn_retire_contract", SOURCE)
assert spec is not None and spec.loader is not None
retire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire)


def test_exact_two_i5_core_scoped_connector_ids() -> None:
    assert retire.CORES == (
        ("i5-core-a", "i5-edge-a", "i7-edge-a", 18873),
        ("i5-core-b", "i5-edge-b", "i7-edge-b", 18874),
    )


def test_exact_native_runtime_evidence_is_retained_after_token_cleanup() -> None:
    stage = {"service.json", "task.xml", "tunnel.token", "identity.json", "runtime.log"}
    retired = {"identity.json", "runtime.log"}
    assert retire.OWNED_ARTIFACTS == {"service.json", "task.xml", "tunnel.token"}
    assert retire.RUNTIME_EVIDENCE == retired
    retire.verify_home_names(stage, retired=False)
    retire.verify_home_names(retired, retired=True)


@pytest.mark.parametrize(
    "names,retired",
    [
        ({"service.json", "task.xml", "tunnel.token"}, False),
        ({"identity.json", "runtime.log", "tunnel.token"}, True),
        ({"identity.json", "runtime.log", "alien.key"}, True),
        (
            {
                "service.json",
                "task.xml",
                "tunnel.token",
                "identity.json",
                "runtime.log",
                "alien.log",
            },
            False,
        ),
        ({"service.json", "task.xml", "tunnel.token", "identity.json", "runtime.log"}, True),
        (set(), True),
    ],
)
def test_native_retirement_rejects_incomplete_or_foreign_artifacts(
    names: set[str], retired: bool
) -> None:
    with pytest.raises(retire.I5PublicRetirementRefusal):
        retire.verify_home_names(names, retired=retired)


def test_retirement_requires_exact_offline_foreign_protected_state() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    for marker in (
        "stage.postflight()",
        'state.get("state") == "Disabled"',
        "_current_identity(manifest) is None",
        "storage.check_private(home / name, directory=False, sid=sid)",
        "verify_home_names({p.name for p in home.iterdir()}, retired=False)",
        "verify_home_names({p.name for p in home.iterdir()}, retired=True)",
        "scheduler.uninstall(manifest)",
        'for filename in ("tunnel.token", "task.xml", "service.json")',
        "path.unlink()",
        "i7.postflight()",
        "i5.verify_candidates(i5.candidates(), installed=True)",
        "I5_PUBLIC_TEMPORARY_CREDENTIALS_REMOVED=PASS",
    ):
        assert marker in source
    for forbidden in (
        "Scheduler().start(",
        "Scheduler().stop(",
        "TerminateProcess",
        "shutil.rmtree(",
        "cloudflare.request(",
        "http.env",
        "ANTHROPIC_API_KEY",
        "git push",
        "os.system(",
    ):
        assert forbidden not in source


def test_retire_does_not_delete_original_i7_token_files() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert (
        'home = storage.home("cloudflare", manifest.instance_id, manifest.connector_id)' in source
    )
    assert 'home = storage.home("cloudflare", core, connector)' in source
    assert 'storage.home("cloudflare", "i7-public"' not in source
    assert "path.unlink()" in source
    assert 'for filename in ("tunnel.token", "task.xml", "service.json")' in source
    assert "identity.json" in source and "runtime.log" in source


def test_sanitized_error_type_only() -> None:
    with pytest.raises(retire.I5PublicRetirementRefusal):
        retire.require(False, "test")
    retire.require(True, "test")
