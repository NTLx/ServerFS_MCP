"""Source-only I11 dual-owned real Claude immutable state diagnostic contracts.

Does not open DB, start any Windows Scheduler task, contact Provider or
inspect private state during pytest; native audit is a separate one-use gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-i11-real-claude-two-owner-history-readonly.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i11_private_history_contract", SOURCE)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize(
    ("instance", "response", "shape"),
    [
        ("i11-b", "I11_OWNED_REAL_A_OK", "EXACT_FOREIGN"),
        ("i11-b", "prefix I11_OWNED_REAL_A_OK suffix", "CONTAINS_FOREIGN"),
        ("i11-b", "other", "NO_FOREIGN"),
        ("i11-a", "I11_OWNED_REAL_B_OK", "EXACT_FOREIGN"),
        ("i11-a", None, "ABSENT"),
        ("i11-a", 123, "INVALID"),
    ],
)
def test_foreign_owner_sentinel_is_classified_without_exposing_output(
    instance, response, shape
) -> None:
    assert _module().foreign_sentinel_shape(response, instance=instance) == shape


@pytest.mark.parametrize(
    ("response", "shape"),
    [
        ("I11_OWNED_REAL_B_OK", "EXACT"),
        ("i11_owned_real_b_ok", "CASE_ONLY"),
        ("I11_OWNED_REAL_B_OK.", "WRAPPER_PUNCTUATION"),
        ("I11_OWNED_REAL_B_ O K", "WHITESPACE_AND_CASE"),
        ("Response: i11_owned_real_b_ok", "CONTAINS_CASE_INSENSITIVE"),
        ("", "EMPTY"),
        ("unrelated", "OTHER_SHORT"),
        ("x" * 150, "OTHER_LONG"),
        (None, "ABSENT"),
        (21, "INVALID"),
    ],
)
def test_own_sentinel_fidelity_is_a_nonsecret_enum(response, shape) -> None:
    mod = _module()
    assert mod.classify_expected_variation(response, mod.SENTINELS["i11-b"]) == shape


def test_exact_two_installed_worker_owner_ids() -> None:
    m = _module()
    assert m.IDS == ("i11-a", "i11-b")
    assert m.SENTINELS == {
        "i11-a": "I11_OWNED_REAL_A_OK",
        "i11-b": "I11_OWNED_REAL_B_OK",
    }


@pytest.mark.parametrize(
    ("response", "shape"),
    [
        (None, "ABSENT"),
        (123, "INVALID"),
        ("I11_OWNED_REAL_A_OK", "EXACT"),
        ('"I11_OWNED_REAL_A_OK"', "QUOTED"),
        ((chr(96) * 3) + "text\nI11_OWNED_REAL_A_OK\n" + (chr(96) * 3), "FENCED"),
        ("prefix I11_OWNED_REAL_A_OK suffix", "CONTAINS_EXTRAS"),
        ("random", "OTHER"),
    ],
)
def test_final_shape_has_only_enum_outputs(response, shape) -> None:
    m = _module()
    assert m.classify_final(response, m.SENTINELS["i11-a"]) == shape


@pytest.mark.parametrize(
    ("final", "message", "relation"),
    [
        (None, "hello", "NO_FINAL"),
        ("a", None, "NO_MESSAGE"),
        ("a", 1, "INVALID_MESSAGE"),
        ("a", "a", "EXACT"),
        ("a", " a ", "WHITESPACE"),
        ("a", "first a last", "FINAL_WITHIN_MESSAGE"),
        ("something and more", "something", "MESSAGE_WITHIN_FINAL"),
        ("alpha", "beta", "DIFFERENT"),
    ],
)
def test_message_relation_categorizes_without_emitting_prose(final, message, relation) -> None:
    assert _module().classify_relation(final, message) == relation


def test_scheduler_query_is_exact_and_nonmutating(monkeypatch) -> None:
    m = _module()
    queries = []

    def fake_run(argv, **kwargs):
        queries.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"I11_BRIDGE_DISABLED", stderr=b"")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m.exact_disabled("i11-a")
    m.exact_disabled("i11-b")
    assert len(queries) == 2
    for id_, pair in zip(m.IDS, queries, strict=True):
        command, options = pair
        assert command[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert "Get-ScheduledTask" in command[-1]
        assert f"ServerFS v25 {id_} Agent Bridge" in command[-1]
        assert "Disabled" in command[-1]
        assert options["check"] is False
        assert options["stdin"] == m.subprocess.DEVNULL
    with pytest.raises(m.I11HistoryRefusal):
        m.exact_disabled("i5-shared")


def test_immutable_readonly_contract_without_provider_or_raw_logs() -> None:
    source = SOURCE.read_text("utf-8")
    for required in (
        "choices=IDS",
        "not ctypes.windll.shell32.IsUserAnAdmin()",
        "Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True)",
        "exact_disabled(instance)",
        "verify_private_file(",
        "require(not wal.exists()",
        'db.as_uri() + "?mode=ro&immutable=1"',
        "PRAGMA query_only=ON",
        "JOIN core_task_owners",
        "WHERE own.core_id=?",
        "LIMIT 2",
        "len(rows) == 1",
        'row["error_code"]',
        'row["final_response"]',
        '"SELECT event_type,payload_json FROM events "',
        "len(events) <= 250",
        "EVENT_TYPES=",
        "OWN_SENTINEL_VARIATION=",
        "LATEST_MESSAGE_VARIATION=",
        "FOREIGN_SENTINEL_SHAPE=",
        "LATEST_MESSAGE_FOREIGN_SHAPE=",
        "FINAL_MESSAGE_RELATION=",
        "PENDING_REQUESTS=",
        "IMMUTABLE_QUERY_ONLY=PASS",
    ):
        assert required in source, required
    for forbidden in (
        "TaskStore(",
        "create_text_file(",
        "update tasks ",
        "delete from ",
        "insert into ",
        "conn.commit(",
        "shutil.rmtree(",
        "Scheduler().install(",
        "Scheduler().start(",
        "Scheduler().stop(",
        "cloudflare.request(",
        "task.submit",
        "subprocess.Popen(",
        "git commit",
        "git push",
        'print(row["task_id"])',
        'print(row["final_response"])',
        'print(row["native_session_id"])',
        "print(raw)",
    ):
        assert forbidden not in source, forbidden
