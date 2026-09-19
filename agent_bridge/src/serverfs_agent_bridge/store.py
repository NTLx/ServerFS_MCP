"""SQLite persistence for Agent Bridge tasks, events and pending requests."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import BridgeError
from .models import (
    TERMINAL_STATUSES,
    BridgeEvent,
    PendingRequest,
    RequestStatus,
    TaskRecord,
    TaskStatus,
)
from .state import validate_transition
from .util import utc_now


class _Unset:
    pass


_UNSET = _Unset()


class TaskStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        try:
            state_stat = self.state_dir.lstat()
        except FileNotFoundError:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            state_stat = self.state_dir.lstat()
        if stat.S_ISLNK(state_stat.st_mode) or not stat.S_ISDIR(state_stat.st_mode):
            raise ValueError("state_dir must be a real directory")
        if state_stat.st_uid != os.getuid() or state_stat.st_mode & 0o077:
            raise ValueError("state_dir must be owned by the bridge user and mode 0700")
        os.chmod(self.state_dir, 0o700)
        self.db_path = self.state_dir / "state.sqlite3"
        try:
            db_stat = self.db_path.lstat()
        except FileNotFoundError:
            db_stat = None
        if db_stat is not None and (
            stat.S_ISLNK(db_stat.st_mode) or not stat.S_ISREG(db_stat.st_mode)
        ):
            raise ValueError("state database path must be a regular file")
        self._initialize()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        try:
            yield con
        except BaseException:
            con.rollback()
            raise
        else:
            con.commit()
        finally:
            con.close()
            self._secure_database_files()

    def _secure_database_files(self) -> None:
        for path in (
            self.db_path,
            self.db_path.with_name("state.sqlite3-wal"),
            self.db_path.with_name("state.sqlite3-shm"),
        ):
            try:
                path_stat = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("state database sidecar must be a regular file")
            os.chmod(path, 0o600)

    def _initialize(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    runtime TEXT NOT NULL,
                    workdir_alias TEXT NOT NULL,
                    workdir_slot INTEGER NOT NULL,
                    relative_cwd TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    continue_from_task_id TEXT,
                    native_session_id TEXT,
                    native_turn_id TEXT,
                    final_response TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    pending_request_id TEXT,
                    event_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS events_task_cursor
                ON events(task_id, event_id);

                CREATE TABLE IF NOT EXISTS pending_requests (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_json TEXT
                );

                CREATE INDEX IF NOT EXISTS pending_requests_task
                ON pending_requests(task_id, status);
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)").fetchall()}
            if "event_count" not in columns:
                con.execute("ALTER TABLE tasks ADD COLUMN event_count INTEGER NOT NULL DEFAULT 0")
                con.execute(
                    """
                    UPDATE tasks
                    SET event_count = (
                        SELECT COUNT(*) FROM events WHERE events.task_id = tasks.task_id
                    )
                    """
                )

    def create_task(
        self,
        *,
        task_id: str,
        runtime: str,
        workdir_alias: str,
        workdir_slot: int,
        relative_cwd: str,
        profile: str,
        continue_from_task_id: str | None,
        max_active_tasks: int | None = None,
    ) -> TaskRecord:
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if max_active_tasks is not None:
                terminal = tuple(status.value for status in TERMINAL_STATUSES)
                placeholders = ",".join("?" for _ in terminal)
                active = con.execute(
                    f"SELECT COUNT(*) AS n FROM tasks WHERE status NOT IN ({placeholders})",
                    terminal,
                ).fetchone()
                if int(active["n"]) >= max_active_tasks:
                    raise BridgeError("AGENT_TASK_LIMIT_REACHED", "too many active agent tasks")
            try:
                con.execute(
                    """
                    INSERT INTO tasks (
                        task_id, runtime, workdir_alias, workdir_slot, relative_cwd,
                        profile, status, created_at, updated_at, continue_from_task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        runtime,
                        workdir_alias,
                        workdir_slot,
                        relative_cwd,
                        profile,
                        TaskStatus.QUEUED.value,
                        now,
                        now,
                        continue_from_task_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError("AGENT_TASK_EXISTS", f"task already exists: {task_id}") from exc
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
        return _task_from_row(row)

    def transition_task(
        self,
        task_id: str,
        target: TaskStatus | str,
        *,
        pending_request_id: str | None | object = _UNSET,
        native_session_id: str | None | object = _UNSET,
        native_turn_id: str | None | object = _UNSET,
        final_response: str | None | object = _UNSET,
        error_code: str | None | object = _UNSET,
        error_message: str | None | object = _UNSET,
    ) -> TaskRecord:
        target_status = TaskStatus(target)
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
            current = TaskStatus(row["status"])
            validate_transition(current, target_status)

            values: dict[str, Any] = {"status": target_status.value, "updated_at": now}
            if current is TaskStatus.QUEUED and target_status is TaskStatus.STARTING:
                values["started_at"] = now
            if target_status in TERMINAL_STATUSES:
                values["completed_at"] = now
                values["pending_request_id"] = None
                if row["pending_request_id"] is not None:
                    con.execute(
                        """
                        UPDATE pending_requests
                        SET status = ?, resolved_at = ?
                        WHERE request_id = ? AND status = ?
                        """,
                        (
                            RequestStatus.STALE.value,
                            now,
                            row["pending_request_id"],
                            RequestStatus.PENDING.value,
                        ),
                    )

            for key, value in {
                "pending_request_id": pending_request_id,
                "native_session_id": native_session_id,
                "native_turn_id": native_turn_id,
                "final_response": final_response,
                "error_code": error_code,
                "error_message": error_message,
            }.items():
                if value is not _UNSET and not (
                    key == "pending_request_id" and target_status in TERMINAL_STATUSES
                ):
                    values[key] = value

            assignments = ", ".join(f"{key} = ?" for key in values)
            con.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?",
                (*values.values(), task_id),
            )
        return self.get_task(task_id)

    def set_native_ids(
        self,
        task_id: str,
        *,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
    ) -> TaskRecord:
        now = utc_now()
        with self._connect() as con:
            row = con.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
            con.execute(
                """
                UPDATE tasks
                SET native_session_id = COALESCE(?, native_session_id),
                    native_turn_id = COALESCE(?, native_turn_id),
                    updated_at = ?
                WHERE task_id = ?
                """,
                (native_session_id, native_turn_id, now, task_id),
            )
        return self.get_task(task_id)

    def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        max_events_per_task: int | None = None,
    ) -> BridgeEvent:
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
            if max_events_per_task is None:
                updated = con.execute(
                    "UPDATE tasks SET event_count = event_count + 1 WHERE task_id = ?",
                    (task_id,),
                )
            else:
                updated = con.execute(
                    """
                    UPDATE tasks
                    SET event_count = event_count + 1
                    WHERE task_id = ? AND event_count < ?
                    """,
                    (task_id, max_events_per_task),
                )
                if updated.rowcount != 1:
                    raise BridgeError("AGENT_EVENT_LIMIT_REACHED", "agent event limit reached")
            cur = con.execute(
                """
                INSERT INTO events(task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, event_type, encoded, now),
            )
            event_id = int(cur.lastrowid)
        return BridgeEvent(event_id, task_id, event_type, payload, now)

    def count_events(self, task_id: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM events WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["n"])

    def list_events(
        self, task_id: str, *, after_event_id: int = 0, limit: int = 100
    ) -> list[BridgeEvent]:
        self.get_task(task_id)
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM events
                WHERE task_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (task_id, after_event_id, limit),
            ).fetchall()
        return [
            BridgeEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def create_pending_request(
        self,
        *,
        task_id: str,
        request_id: str,
        kind: str,
        payload: dict[str, Any],
        waiting_status: TaskStatus,
    ) -> PendingRequest:
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
            if task["pending_request_id"] is not None:
                raise BridgeError("REQUEST_PENDING", "task already has a pending request")
            validate_transition(task["status"], waiting_status)
            try:
                con.execute(
                    """
                    INSERT INTO pending_requests(
                        request_id, task_id, kind, status, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        task_id,
                        kind,
                        RequestStatus.PENDING.value,
                        encoded,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BridgeError(
                    "REQUEST_EXISTS", f"request already exists: {request_id}"
                ) from exc
            con.execute(
                """
                UPDATE tasks
                SET status = ?, pending_request_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (waiting_status.value, request_id, now, task_id),
            )
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> PendingRequest:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM pending_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise BridgeError("REQUEST_NOT_FOUND", f"unknown request: {request_id}")
        return _request_from_row(row)

    def resolve_request(self, request_id: str, resolution: dict[str, Any]) -> PendingRequest:
        now = utc_now()
        encoded = json.dumps(resolution, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            req = con.execute(
                "SELECT * FROM pending_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if req is None:
                raise BridgeError("REQUEST_NOT_FOUND", f"unknown request: {request_id}")
            if req["status"] != RequestStatus.PENDING.value:
                raise BridgeError("REQUEST_ALREADY_RESOLVED", "request is no longer pending")
            task = con.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (req["task_id"],)
            ).fetchone()
            if task is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {req['task_id']}")
            if task["pending_request_id"] != request_id:
                raise BridgeError("REQUEST_STALE", "request is no longer active for the task")
            validate_transition(task["status"], TaskStatus.RUNNING)
            request_update = con.execute(
                """
                UPDATE pending_requests
                SET status = ?, resolved_at = ?, resolution_json = ?
                WHERE request_id = ? AND status = ?
                """,
                (
                    RequestStatus.RESOLVED.value,
                    now,
                    encoded,
                    request_id,
                    RequestStatus.PENDING.value,
                ),
            )
            if request_update.rowcount != 1:
                raise BridgeError("REQUEST_ALREADY_RESOLVED", "request is no longer pending")
            task_update = con.execute(
                """
                UPDATE tasks
                SET status = ?, pending_request_id = NULL, updated_at = ?
                WHERE task_id = ? AND pending_request_id = ?
                """,
                (TaskStatus.RUNNING.value, now, req["task_id"], request_id),
            )
            if task_update.rowcount != 1:
                raise BridgeError("REQUEST_STALE", "request is no longer active for the task")
        return self.get_request(request_id)

    def count_nonterminal_tasks(self) -> int:
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as con:
            row = con.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE status NOT IN ({placeholders})",
                terminal,
            ).fetchone()
        return int(row["n"])

    def stale_task_request(self, task_id: str) -> None:
        now = utc_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute(
                "SELECT pending_request_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise BridgeError("AGENT_TASK_NOT_FOUND", f"unknown task: {task_id}")
            request_id = task["pending_request_id"]
            if request_id is not None:
                con.execute(
                    """
                    UPDATE pending_requests
                    SET status = ?, resolved_at = ?
                    WHERE request_id = ? AND status = ?
                    """,
                    (
                        RequestStatus.STALE.value,
                        now,
                        request_id,
                        RequestStatus.PENDING.value,
                    ),
                )
                con.execute(
                    """
                    UPDATE tasks
                    SET pending_request_id = NULL, updated_at = ?
                    WHERE task_id = ? AND pending_request_id = ?
                    """,
                    (now, task_id, request_id),
                )

    def interrupt_nonterminal_tasks(self) -> int:
        now = utc_now()
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                f"SELECT task_id FROM tasks WHERE status NOT IN ({placeholders})", terminal
            ).fetchall()
            task_ids = [row["task_id"] for row in rows]
            if task_ids:
                id_placeholders = ",".join("?" for _ in task_ids)
                con.execute(
                    f"""
                    UPDATE tasks
                    SET status = ?, completed_at = ?, updated_at = ?,
                        pending_request_id = NULL,
                        error_code = COALESCE(error_code, 'BRIDGE_RESTARTED'),
                        error_message = COALESCE(
                            error_message,
                            'bridge restarted while task was active'
                        )
                    WHERE task_id IN ({id_placeholders})
                    """,
                    (TaskStatus.INTERRUPTED.value, now, now, *task_ids),
                )
                con.execute(
                    f"""
                    UPDATE pending_requests
                    SET status = ?, resolved_at = ?
                    WHERE task_id IN ({id_placeholders}) AND status = ?
                    """,
                    (RequestStatus.STALE.value, now, *task_ids, RequestStatus.PENDING.value),
                )
        return len(task_ids)


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        runtime=row["runtime"],
        workdir_alias=row["workdir_alias"],
        workdir_slot=row["workdir_slot"],
        relative_cwd=row["relative_cwd"],
        profile=row["profile"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        continue_from_task_id=row["continue_from_task_id"],
        native_session_id=row["native_session_id"],
        native_turn_id=row["native_turn_id"],
        final_response=row["final_response"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        pending_request_id=row["pending_request_id"],
    )


def _request_from_row(row: sqlite3.Row) -> PendingRequest:
    return PendingRequest(
        request_id=row["request_id"],
        task_id=row["task_id"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolution=json.loads(row["resolution_json"]) if row["resolution_json"] else None,
    )
