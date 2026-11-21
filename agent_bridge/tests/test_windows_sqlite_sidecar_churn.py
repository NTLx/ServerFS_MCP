"""Native Windows SQLite sidecar churn regression using the real Bridge store.

The test never contacts a provider or an installed service. Four threads
repeatedly open/close actual SQLite connections beneath the owner-only DACL,
exercising concurrent WAL/SHM teardown rather than mocking file existence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from platform_contract import require_windows_kernel
from serverfs_agent_bridge.store import TaskStore

require_windows_kernel("real NTFS sidecar ACL and SQLite handle lifecycle required")


def test_concurrent_read_connections_do_not_fail_on_vanishing_sidecars(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    store.create_task(
        task_id="agt_acl_churn",
        runtime="fake",
        workdir_alias="repo",
        workdir_slot=1,
        relative_cwd="",
        profile="review",
        continue_from_task_id=None,
    )

    def read_many(_: int) -> int:
        for _step in range(60):
            task = store.get_task("agt_acl_churn")
            assert task.status == "queued"
        return 60

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(read_many, range(4))) == 240

    store._secure_database_files()
    assert store.get_task("agt_acl_churn").status == "queued"
