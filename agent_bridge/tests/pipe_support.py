"""Harness for the Windows Named-Pipe local-IPC tests (v0.11 Phase B, §36).

The transport under test is a blocking Win32 object, so the RPC server runs on its own
event-loop thread and the clients are ordinary synchronous helpers driven from the test
thread (or from real threads for the concurrent cases). Everything here is test-only: the
pipe name carries a random suffix so a test can never collide with a deployed Bridge, and
the production SID-derived name is asserted separately.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from serverfs_agent_bridge.adapters import FakeAdapter
from serverfs_agent_bridge.leases import LeaseManager
from serverfs_agent_bridge.models import AgentMode
from serverfs_agent_bridge.policy import PolicyRegistry, WorkdirAgentPolicy
from serverfs_agent_bridge.protocol import PROTOCOL_VERSION, BridgeProtocolServer
from serverfs_agent_bridge.service import BridgeService
from serverfs_agent_bridge.store import TaskStore
from serverfs_agent_bridge.windows_pipe import (
    ERROR_FILE_NOT_FOUND,
    ERROR_PIPE_BUSY,
    ERROR_PIPE_NOT_CONNECTED,
    client_read,
    client_write,
    close_handle,
    open_client,
    pipe_peer_ids,
    process_user_sid,
)

#: §14 — the three conditions Phase 0B measured as worth another attempt inside the
#: request timeout. Anything else is returned to the caller immediately.
TRANSIENT_OPEN_ERRORS = frozenset({ERROR_PIPE_BUSY, ERROR_PIPE_NOT_CONNECTED, ERROR_FILE_NOT_FOUND})


def new_pipe_name() -> str:
    return f"\\\\.\\pipe\\serverfs-bridge-test-{secrets.token_hex(8)}"


def make_service(tmp_path: Path, *, workspace_write: bool = False) -> BridgeService:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    # A native deployment carries no slots (§5.3), so this fixture keys the lease the way the
    # platform's own deployment does, and pre-creates exactly that artifact.
    policy = WorkdirAgentPolicy(
        slot=None if sys.platform == "win32" else 1,
        alias="repo",
        host_path=repo,
        mode=AgentMode.WORKSPACE_WRITE if workspace_write else AgentMode.REVIEW,
        runtimes=frozenset({"fake"}),
        read_only=not workspace_write,
    )
    return BridgeService(
        store=TaskStore(tmp_path / "state"),
        policies=PolicyRegistry([policy]),
        adapters={"fake": FakeAdapter()},
        lease_manager=LeaseManager(tmp_path / "locks", lease_ids=[policy.lease_id]),
    )


class RunningBridge:
    """A real ``BridgeProtocolServer`` on a dedicated event-loop thread."""

    def __init__(
        self,
        service: BridgeService,
        *,
        pipe_name: str | None = None,
        allowed_peer_sid: str | None = None,
    ):
        self.pipe_name = pipe_name or new_pipe_name()
        self.service = service
        self.server = BridgeProtocolServer(
            service=service,
            socket_path=service.store.state_dir / "endpoint",
            allowed_peer_sid=allowed_peer_sid,
            pipe_name=self.pipe_name,
        )
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._serve_task: asyncio.Task | None = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self, *, timeout_seconds: float = 15.0) -> RunningBridge:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._amain(), self._loop)
        if not self._ready.wait(timeout_seconds):
            raise AssertionError("the Bridge pipe server never became ready")
        if self._failure is not None:
            raise self._failure
        return self

    async def _amain(self) -> None:
        try:
            await self.service.start()
            await self.server.start()
            self._serve_task = asyncio.get_running_loop().create_task(self.server.serve_forever())
        except BaseException as exc:  # reported to start(), never swallowed by the thread
            self._failure = exc
        finally:
            self._ready.set()

    def stop(self, *, timeout_seconds: float = 15.0) -> None:
        asyncio.run_coroutine_threadsafe(self._astop(), self._loop).result(timeout_seconds)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout_seconds)

    async def _astop(self) -> None:
        await self.server.close()
        if self._serve_task is not None:
            self._serve_task.cancel()
            await asyncio.gather(self._serve_task, return_exceptions=True)
        await self.service.close()

    def __enter__(self) -> RunningBridge:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


class PipeClient:
    """Synchronous client over the measured byte-pipe framing: one JSON line per frame."""

    def __init__(self, name: str, *, deadline_seconds: float = 15.0):
        self.name = name
        self.deadline_seconds = deadline_seconds
        self.handle = 0
        self._buffer = bytearray()
        self.eof = False

    def connect(self) -> None:
        """Open with the bounded backoff §14 allows, inside one overall deadline."""
        delay = 0.005
        deadline = time.monotonic() + self.deadline_seconds
        while True:
            handle, error = open_client(self.name)
            if handle:
                self.handle = handle
                return
            if error not in TRANSIENT_OPEN_ERRORS or time.monotonic() >= deadline:
                raise ConnectionError(f"pipe open failed with Win32 error {error}")
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.1)

    def write(self, payload: bytes) -> None:
        error = client_write(self.handle, payload)
        if error:
            raise ConnectionError(f"pipe write failed with Win32 error {error}")

    def read_frame(self) -> dict[str, Any]:
        return json.loads(self.read_line())

    def read_line(self) -> bytes:
        """Read until the newline, keeping bytes for the next frame (§16)."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            if self.eof:
                return bytes(self._buffer)
            data, error = client_read(self.handle, 65536)
            if error or not data:
                self.eof = True
                continue
            self._buffer.extend(data)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or f"t_{secrets.token_hex(6)}"
        self.write(request_frame(request_id, method, params))
        return self.read_frame()

    def close(self) -> None:
        if self.handle:
            close_handle(self.handle)
            self.handle = 0

    def __enter__(self) -> PipeClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- §12 measurements the client is required to take ----

    def server_pid(self) -> int:
        pid, _session, error = pipe_peer_ids(self.handle, server_side=True)
        if error:
            raise ConnectionError(f"server pid could not be measured (Win32 error {error})")
        return pid

    def server_sid(self) -> str:
        sid, error = process_user_sid(self.server_pid())
        if sid is None:
            raise ConnectionError(f"server SID could not be measured (Win32 error {error})")
        return sid


def request_frame(request_id: str, method: str, params: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "method": method,
                "params": params,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def padded_request_frame(request_id: str, total_bytes: int) -> bytes:
    """A valid frame whose full length, newline included, is exactly ``total_bytes``.

    ``runtime.models`` advice takes a free-text ``task_prompt``, so the padding lives in a real
    optional parameter and the boundary frame is still a dispatchable request.
    """
    minimum = {"runtime": "fake", "workdir": "repo", "path": "", "profile": "review"}
    frame = request_frame(request_id, "runtime.models", {**minimum, "task_prompt": ""})
    padding = total_bytes - len(frame)
    if padding < 0:
        raise ValueError(f"{total_bytes} is below the minimum frame size")
    frame = request_frame(request_id, "runtime.models", {**minimum, "task_prompt": "x" * padding})
    if len(frame) != total_bytes:
        raise AssertionError(f"frame is {len(frame)} bytes, expected {total_bytes}")
    return frame


def wait_until(predicate, *, timeout_seconds: float = 10.0, interval: float = 0.02) -> bool:
    """Poll a condition that the pipe teardown completes asynchronously."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
