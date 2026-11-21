"""Native no-provider stress for the *installed* I5 Bridge Worker SQLite patch.

All mutations are confined to a disposable Windows system-temp directory.
The installed i5-shared Bridge and both Cores must be Disabled. No Scheduler
lifecycle actions, network, provider, model or production state access.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKERS = 4
ROUNDS = 75
_QUERY = r"""
$ErrorActionPreference='Stop'
$names=@(
 'ServerFS v25 i5-shared Agent Bridge',
 'ServerFS v25 i5-core-a Core',
 'ServerFS v25 i5-core-b Core'
)
$t=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {
 $_.TaskPath -eq '\' -and $_.TaskName -in $names
})
if($t.Count -ne 3 -or @($t | Where-Object {
 [string]$_.State -ne 'Disabled' -or $_.Settings.Enabled
}).Count -ne 0){exit 9}
[Console]::Out.Write('I5_FIXTURES_DISABLED')
"""


class I5WorkerStressError(RuntimeError):
    """Public result never contains a private path, token or task payload."""


def require(value: bool, message: str) -> None:
    if not value:
        raise I5WorkerStressError(message)


def main() -> None:
    import serverfs_agent_bridge.store as installed_store
    from serverfs_agent_bridge import private_state
    from serverfs_agent_bridge.store import TaskStore

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "native ordinary-user installed Worker Python -I -B required",
    )
    base = Path(os.environ["LOCALAPPDATA"]) / "ServerFS" / "v25" / "instances" / "i5-shared"
    expected = base / "envs" / "agent-bridge" / "Scripts" / "python.exe"
    source = (
        base
        / "envs"
        / "agent-bridge"
        / "Lib"
        / "site-packages"
        / "serverfs_agent_bridge"
        / "store.py"
    )
    require(
        Path(sys.executable).resolve(strict=True) == expected.resolve(strict=True)
        and Path(installed_store.__file__).resolve(strict=True) == source.resolve(strict=True),
        "not running the installed private I5 Bridge Worker package",
    )
    private_state.verify_private_file(
        source, not_regular="installed Worker is unsafe", not_private="installed Worker ACL invalid"
    )
    print("I5_WORKER_STRESS_INSTALLED_PACKAGE=VERIFIED")

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    require(
        result.returncode == 0 and result.stdout.strip() == b"I5_FIXTURES_DISABLED",
        "I5 protected fixture tasks are not Disabled",
    )
    print("I5_WORKER_STRESS_THREE_FIXTURES=DISABLED")

    with tempfile.TemporaryDirectory(prefix="serverfs-i5-worker-") as directory:
        store = TaskStore(Path(directory) / "state")
        store.create_task(
            task_id="agt_native_sqlite_race",
            runtime="fake",
            workdir_alias="race-fixture",
            workdir_slot=1,
            relative_cwd="",
            profile="review",
            continue_from_task_id=None,
        )

        def read_many(_: int) -> int:
            for _round in range(ROUNDS):
                require(
                    store.get_task("agt_native_sqlite_race").status == "queued",
                    "transient ACL error or task drift during SQLite churn",
                )
            return ROUNDS

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            require(
                sum(pool.map(read_many, range(WORKERS))) == WORKERS * ROUNDS,
                "SQLite concurrent execution incomplete",
            )
        store._secure_database_files()
        private_state.verify_private_file(
            store.db_path, not_regular="SQLite database unsafe", not_private="SQLite ACL unsafe"
        )
    print("I5_WORKER_STRESS_SQLITE_CONNECTIONS=PASS")
    print("I5_WORKER_STRESS_TEMP_SCOPE=CLEANED")
    print("I5_INSTALLED_WAL_SHM_CHURN=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - fixed exception class only
        print("I5_INSTALLED_WAL_SHM_CHURN=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
