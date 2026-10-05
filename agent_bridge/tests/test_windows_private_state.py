"""Windows private-state tests — the §37 matrix for the PRIVATE_STATE seam.

These cases assert the contract Phase 0B/§6 requires of the Windows twin: every object the
Bridge creates is created with an explicit protected descriptor naming exactly the Bridge user,
every object it did not create is verified and refused rather than repaired, and the state,
results, locks, active-guard and SQLite sidecar paths all sit inside that boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from platform_contract import require_windows_kernel
from serverfs_agent_bridge import lease_identity, private_state, windows_security
from serverfs_agent_bridge.data_home import bridge_data_home, serverfs_data_dir
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.recovery import ActiveGuardManager
from serverfs_agent_bridge.result_spool import ResultSpool
from serverfs_agent_bridge.store import TaskStore

require_windows_kernel("private state is authorized by owner SID, NTFS DACL and reparse state")

FOREIGN_SID = "S-1-5-21-1-1-1-4242"
EVERYONE = "S-1-1-0"
LOCAL_SID = windows_security.current_user_sid()
# A pre-planted object has to be creatable at all, so these descriptors keep the creator's own
# grant and add the one that must be refused.
BROAD_SDDL = f"D:(A;;GA;;;{LOCAL_SID})(A;;FR;;;WD)"
FOREIGN_GRANT_SDDL = f"D:(A;;GA;;;{LOCAL_SID})(A;;GA;;;{FOREIGN_SID})"


def messages() -> private_state.DirectoryMessages:
    return private_state.DirectoryMessages(
        not_a_directory="state dir must be a real directory",
        not_owned="state dir must be owned by the bridge user",
    )


def expect_bridge_error(code: str, call) -> BridgeError:
    with pytest.raises(BridgeError) as raised:
        call()
    assert raised.value.code == code, f"expected {code}, got {raised.value.code}"
    return raised.value


def security(path: Path) -> windows_security.ObjectSecurity:
    return windows_security.read_object_security(path)


# ---- §22/§23 data home ----


def test_default_data_home_is_localappdata_serverfs(monkeypatch) -> None:
    local = Path(os.environ["LOCALAPPDATA"])
    monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
    assert serverfs_data_dir() == local / "ServerFS"
    assert bridge_data_home() == local / "ServerFS" / "agent-bridge"


def test_serverfs_data_home_override_is_exact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "home"))
    assert serverfs_data_dir() == tmp_path / "home"
    assert bridge_data_home() == tmp_path / "home" / "agent-bridge"


def test_missing_localappdata_without_override_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    expect_bridge_error("DATA_HOME_UNAVAILABLE", lambda: bridge_data_home())


def test_data_home_never_falls_back_to_cwd_or_temp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    expect_bridge_error("DATA_HOME_UNAVAILABLE", lambda: bridge_data_home())
    assert list(tmp_path.iterdir()) == []


# ---- §24/§25/§27 creation contract ----


def test_new_directory_is_created_protected_and_grants_only_the_bridge_user(
    tmp_path: Path,
) -> None:
    expected = windows_security.current_user_sid()
    target = tmp_path / "state"
    private_state.ensure_private_directory(target, mode=0o700, messages=messages(), parents=True)
    descriptor = security(target)
    assert descriptor.owner_sid == expected
    assert descriptor.dacl_present
    assert descriptor.dacl_protected
    assert descriptor.broad_trustee() is None
    assert [ace["sid"] for ace in descriptor.aces] == [expected]
    assert descriptor.grants(expected)


def test_new_file_is_created_protected_and_grants_only_the_bridge_user(
    tmp_path: Path,
) -> None:
    expected = windows_security.current_user_sid()
    target = tmp_path / "guard.json"
    private_state.ensure_private_file(
        target, mode=0o600, not_regular="state file must be a regular file"
    )
    descriptor = security(target)
    assert descriptor.owner_sid == expected
    assert descriptor.dacl_protected
    assert [ace["sid"] for ace in descriptor.aces] == [expected]


def test_ancestors_created_by_the_bridge_are_protected_too(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "state"
    private_state.ensure_private_directory(nested, mode=0o700, messages=messages(), parents=True)
    for directory in (nested, nested.parent, nested.parent.parent):
        descriptor = security(directory)
        assert descriptor.dacl_present
        assert descriptor.broad_trustee() is None


def test_existing_insecure_directory_fails_closed_instead_of_being_repaired(
    tmp_path: Path,
) -> None:
    """§25: a pre-planted state path is refused, not quietly re-secured."""
    planted = tmp_path / "state"
    assert windows_security.create_private_directory(planted, BROAD_SDDL)
    error = expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.ensure_private_directory(planted, mode=0o700, messages=messages()),
    )
    assert "grants access to" in error.message or "another trustee" in error.message
    # The refusal must not have rewritten the planted descriptor.
    assert security(planted).broad_trustee() == windows_security.BANNED_TRUSTEES[EVERYONE]


def test_existing_file_granting_another_trustee_fails_closed(tmp_path: Path) -> None:
    planted = tmp_path / "guard.json"
    assert windows_security.create_private_file(planted, FOREIGN_GRANT_SDDL)
    expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.ensure_private_file(
            planted, mode=0o600, not_regular="state file must be a regular file"
        ),
    )


def test_foreign_expected_owner_sid_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§26: the SID string is the authority, so a mismatched expectation stops the Bridge."""
    target = tmp_path / "state"
    private_state.ensure_private_directory(target, mode=0o700, messages=messages())
    monkeypatch.setattr(windows_security, "current_user_sid", lambda: FOREIGN_SID)
    error = expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.verify_private_file(target, not_regular="nope", not_private="nope"),
    )
    assert "another owner" in error.message


def test_security_query_failure_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    expect_bridge_error(
        "PRIVATE_STATE_CHECK_FAILED", lambda: windows_security.read_object_security(missing)
    )


# ---- §28 reparse defense ----


def make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(f"junction creation is unavailable here: {result.stderr.strip()}")


def test_junction_as_state_directory_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "state"
    make_junction(junction, real)
    assert windows_security.is_reparse_point(junction)
    expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.ensure_private_directory(junction, mode=0o700, messages=messages()),
    )


def test_junction_below_a_state_parent_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    state = tmp_path / "state"
    private_state.ensure_private_directory(state, mode=0o700, messages=messages())
    junction = state / "results"
    make_junction(junction, real)
    spool = state / "results"
    expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.ensure_private_directory(spool, mode=0o700, messages=messages()),
    )


def test_symlink_as_final_state_object_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "guard.json"
    try:
        os.symlink(str(target), str(link))
    except OSError as exc:
        pytest.skip(f"file symlink creation needs Developer Mode or privilege: {exc}")
    assert windows_security.is_reparse_point(link)
    expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.require_regular_file(
            link, not_regular="state file must be a regular file"
        ),
    )


def test_state_parent_under_a_junction_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "bridge"
    make_junction(junction, real)
    nested = junction / "state"
    expect_bridge_error(
        "PRIVATE_STATE_UNSAFE",
        lambda: private_state.ensure_private_directory(
            nested, mode=0o700, messages=messages(), parents=True
        ),
    )


# ---- §29 SQLite and sidecar confinement ----


def test_task_store_creates_a_protected_state_tree(tmp_path: Path) -> None:
    expected = windows_security.current_user_sid()
    store = TaskStore(tmp_path / "state")
    TaskStore(tmp_path / "state").create_task(
        task_id="agt_win",
        runtime="fake",
        workdir_alias="repo",
        workdir_slot=1,
        relative_cwd="",
        profile="review",
        continue_from_task_id=None,
    )
    descriptor = security(store.state_dir)
    assert descriptor.owner_sid == expected
    assert descriptor.broad_trustee() is None
    assert security(store.db_path).grants(expected)


def test_sqlite_wal_and_shm_companions_stay_confined(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    expected = windows_security.current_user_sid()
    with store._connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT 1")
    for name in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
        companion = store.state_dir / name
        if not companion.exists():
            continue
        descriptor = security(companion)
        assert descriptor.owner_sid == expected, name
        assert descriptor.broad_trustee() is None, name
        assert [ace["sid"] for ace in descriptor.aces] == [expected], name


def test_planted_sidecar_is_refused_rather_than_repaired(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    planted = store.state_dir / "state.sqlite3-wal"
    assert windows_security.create_private_file(planted, FOREIGN_GRANT_SDDL)
    expect_bridge_error("PRIVATE_STATE_UNSAFE", lambda: store._secure_database_files())


def test_store_refuses_a_state_dir_pre_planted_insecurely(tmp_path: Path) -> None:
    planted = tmp_path / "state"
    assert windows_security.create_private_directory(planted, BROAD_SDDL)
    expect_bridge_error("PRIVATE_STATE_UNSAFE", lambda: TaskStore(planted))


# ---- §30 result spool ----


def test_result_spool_directory_and_files_are_protected(tmp_path: Path) -> None:
    expected = windows_security.current_user_sid()
    state = tmp_path / "state"
    private_state.ensure_private_directory(state, mode=0o700, messages=messages())
    spool = ResultSpool(state)
    metadata = spool.write("agt_win", "x" * (300 * 1024))
    assert metadata.size_bytes == 300 * 1024
    results_dir = state / "results"
    assert security(results_dir).dacl_protected
    written = results_dir / "agt_win.txt"
    descriptor = security(written)
    assert descriptor.owner_sid == expected
    assert [ace["sid"] for ace in descriptor.aces] == [expected]


def test_spooled_result_reads_back_through_the_seam(tmp_path: Path) -> None:
    state = tmp_path / "state"
    private_state.ensure_private_directory(state, mode=0o700, messages=messages())
    spool = ResultSpool(state)
    text = "\N{CJK UNIFIED IDEOGRAPH-4E2D}" + "x" * (300 * 1024)
    spool.write("agt_win", text)
    chunk = spool.read_chunk("agt_win", offset_bytes=0, max_bytes=65_536)
    assert chunk["eof"] is False
    assert chunk["next_offset_bytes"] == len(chunk["text"].encode("utf-8"))
    remaining = spool.read_chunk(
        "agt_win", offset_bytes=chunk["next_offset_bytes"], max_bytes=65_536
    )
    assert remaining["text"]
    spool.delete("agt_win")
    assert not (state / "results" / "agt_win.txt").exists()


# ---- active guards and the Phase C boundary ----


def test_active_guard_directory_and_guard_file_are_protected(tmp_path: Path) -> None:
    expected = windows_security.current_user_sid()
    locks = tmp_path / "locks"
    private_state.ensure_private_directory(locks, mode=0o700, messages=messages(), parents=True)
    manager = ActiveGuardManager(locks)
    lease_id = lease_identity.alias_lease_id("repo")
    manager.create(
        lease_id=lease_id,
        task_id="agt_win",
        runtime="fake",
        workdir_alias="repo",
        correlation_id=None,
    )
    guard_path = manager._path(lease_id)
    assert security(manager.guard_dir).owner_sid == expected
    descriptor = security(guard_path)
    assert descriptor.owner_sid == expected
    assert descriptor.broad_trustee() is None


def test_shared_gid_private_state_fails_closed_on_windows(tmp_path: Path) -> None:
    expect_bridge_error(
        "BRIDGE_PLATFORM_UNSUPPORTED",
        lambda: private_state.ensure_private_directory(
            tmp_path / "locks",
            mode=0o750,
            shared_gid=os.getuid() if hasattr(os, "getuid") else 0,
            messages=messages(),
        ),
    )


def test_lease_artifact_is_private_state_and_is_leaseable(tmp_path: Path) -> None:
    """Phase C replaced the WRITER_LEASE guard: the artifact is private state, and it locks.

    The lease is the one §3 seam whose artifact the Bridge both creates and holds, so it gets the
    same per-object descriptor as every other state file — never one inherited from the directory,
    which Phase 0A measured as silently denying the reader's open.
    """
    expected = windows_security.current_user_sid()
    lease_id = lease_identity.alias_lease_id("repo")
    manager = LeaseManager(tmp_path / "locks", lease_ids=[lease_id])
    artifact = manager.lock_dir / lease_identity.lock_artifact_name(lease_id)
    assert artifact.is_file()
    descriptor = security(artifact)
    assert descriptor.owner_sid == expected
    assert descriptor.broad_trustee() is None

    held = manager.acquire_exclusive(lease_id)
    with pytest.raises(BridgeError) as busy:
        manager.acquire_exclusive(lease_id)
    assert busy.value.code == "WORKDIR_BUSY"
    held.release()
    manager.acquire_exclusive(lease_id).release()


def test_unprepared_lease_artifact_fails_closed(tmp_path: Path) -> None:
    """Acquisition never creates: only startup pre-creation of a configured lease may."""
    manager = LeaseManager(tmp_path / "locks")
    expect_bridge_error(
        "LOCK_PATH_UNSAFE",
        lambda: manager.acquire_exclusive(lease_identity.alias_lease_id("repo")),
    )


def test_private_state_module_reports_windows() -> None:
    assert private_state.WINDOWS is (sys.platform == "win32")
