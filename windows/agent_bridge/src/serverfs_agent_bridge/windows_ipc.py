"""The Windows local-IPC endpoint (§4, §6–§18) behind the §3 seam.

Imported lazily by ``local_ipc.create_pipe_endpoint`` so a Linux Bridge process never loads a
Win32 module and the frozen Linux path stays exactly as it is.

The shape implemented here is what Phase 0B measured, item by item:

- byte mode plus a persistent frame buffer, because two frames written in one ``WriteFile``
  arrive together and one frame can be split across reads;
- an explicit protected DACL on every instance, because the default pipe descriptor was measured
  granting read access to Everyone and Anonymous;
- ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the process's first instance, because without it a second
  process silently joins the name and steals connections;
- the peer measured after the first completed read — ``ImpersonateNamedPipeClient`` fails with
  ``ERROR_CANT_IMPERSONATE_NAMED_PIPE`` before one — and asserted before the frame is returned
  to the RPC core, so an unverified peer can never be dispatched;
- a bounded pool of listening instances, each slot replenished when its connection finishes,
  because a byte pipe serves at most as many simultaneous clients as there are listeners;
- readiness reported only once an instance is actually inside ``ConnectNamedPipe``, because a
  created-but-never-connected instance makes client opens block indefinitely;
- a bounded idle read, because a client that never sends a newline must not pin an instance.

No blocking Win32 call runs on the event loop: one accept thread per pool slot owns the connect,
and frame reads and writes run on an executor sized to the pool, so one stalled peer can neither
create threads without limit nor outlive its own timeout.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

from .errors import BridgeError
from .local_ipc import PEER_INFO_KEY, WindowsPeer
from .windows_pipe import (
    ERROR_ACCESS_DENIED,
    ERROR_IO_PENDING,
    ServerInstance,
)

_READ_CHUNK = 65536
_ACCEPT_POLL_MS = 50
_START_DEADLINE_SECONDS = 10.0
_JOIN_DEADLINE_SECONDS = 10.0


class PipeConnection:
    """One connected instance plus the framing the shared RPC core expects.

    ``readline`` keeps the Linux semantics the RPC core relies on: a whole line, or ``b""`` at
    end of stream. One deliberate difference — a line over the limit is returned capped just past
    it instead of raising ``LimitOverrunError``, so the shared ``len(raw) > MAX_REQUEST_BYTES``
    check produces the same coded ``REQUEST_TOO_LARGE`` outcome on both platforms.
    """

    def __init__(
        self,
        *,
        endpoint: NamedPipeEndpoint,
        instance: ServerInstance,
        executor: concurrent.futures.Executor,
    ):
        self.endpoint = endpoint
        self.instance = instance
        self.peer: WindowsPeer | None = None
        self.reader = _PipeReader(self, executor)
        self.writer = _PipeWriter(self, executor)
        self._buffer = bytearray()
        self._closed = threading.Event()

    def read_frame(self) -> bytes:
        limit = self.endpoint.limit
        while not self._closed.is_set():
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            if len(self._buffer) > limit:
                oversized = bytes(self._buffer[: limit + 1])
                self._buffer.clear()
                self.request_close()
                return oversized
            data, why = self.instance.read(
                _READ_CHUNK, timeout_ms=self.endpoint.idle_timeout_ms, abort=self._closed
            )
            if why != "data":
                leftover = bytes(self._buffer)
                self._buffer.clear()
                self.request_close()
                return leftover
            if self.peer is None:
                self._measure_peer()
            self._buffer.extend(data)
        return b""

    def write_frame(self, payload: bytes) -> str:
        if self._closed.is_set():
            return "closed"
        return self.instance.write(
            payload, timeout_ms=self.endpoint.idle_timeout_ms, abort=self._closed
        )

    def _measure_peer(self) -> None:
        """Measure the client now that a read has completed.

        A failure leaves ``peer`` unset and closes the connection: the assertion in
        ``protocol.py`` refuses anything unmeasured, and this frame has not reached a
        dispatcher.
        """
        try:
            identity = self.instance.measure_peer()
        except BridgeError:
            self.request_close()
            return
        self.peer = WindowsPeer(
            sid=identity.sid,
            pid=identity.process_id,
            session_id=identity.session_id,
            impersonation_level=identity.impersonation_level,
            token_type=identity.token_type,
        )

    def request_close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.instance.cancel_pending()

    def dispose(self) -> None:
        self.request_close()
        self.instance.close()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


class _PipeReader:
    def __init__(self, connection: PipeConnection, executor: concurrent.futures.Executor):
        self._connection = connection
        self._executor = executor

    async def readline(self) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._connection.read_frame
        )


class _PipeWriter:
    """Writer half exposing the surface ``protocol.py`` already uses."""

    def __init__(self, connection: PipeConnection, executor: concurrent.futures.Executor):
        self._connection = connection
        self._executor = executor
        self._pending = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pending.extend(data)

    async def drain(self) -> None:
        if not self._pending:
            return
        payload = bytes(self._pending)
        self._pending = bytearray()
        why = await asyncio.get_running_loop().run_in_executor(
            self._executor, self._connection.write_frame, payload
        )
        if why != "ok":
            raise ConnectionError("the Bridge pipe response could not be written")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.request_close()

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, key: str, default: Any = None) -> Any:
        if key == PEER_INFO_KEY:
            return self._connection.peer
        return default


class NamedPipeEndpoint:
    """A bounded pool of listening pipe instances, serving one connection per slot."""

    def __init__(
        self,
        *,
        pipe_name: str,
        dacl_sddl: str,
        limit: int,
        pool_size: int = 4,
        idle_timeout_seconds: float = 30.0,
    ):
        if pool_size < 2:
            raise ValueError("the Bridge pipe instance pool needs at least two listeners")
        if idle_timeout_seconds <= 0:
            raise ValueError("the Bridge pipe idle timeout must be positive")
        self.pipe_name = pipe_name
        self.limit = limit
        self.pool_size = pool_size
        self.idle_timeout_ms = max(1, int(idle_timeout_seconds * 1000))
        self._dacl_sddl = dacl_sddl
        # One worker per pool slot is the bound on concurrent frame reads and writes.
        self._io = concurrent.futures.ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="serverfs-bridge-pipe"
        )
        self._stop = threading.Event()
        self._listening = threading.Event()
        self._name_taken = threading.Event()
        self._threads: list[threading.Thread] = []
        self._connections: set[PipeConnection] = set()
        self._connections_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_connection: Any = None
        self._first_instance: ServerInstance | None = None

    # ---- identity ----

    def peer(self, writer: Any) -> WindowsPeer | None:
        return writer.get_extra_info(PEER_INFO_KEY)

    # ---- lifecycle ----

    async def start(self, on_connection: Any) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_connection = on_connection
        # The name is claimed here, before any pool thread exists, so the first-instance flag is
        # deterministic: with four threads racing, a slot that created a plain instance first would
        # make this process's own claim fail, and a collision could then only ever be reported as
        # a guess (§7).
        instance, code = await self._loop.run_in_executor(self._io, self._claim_name)
        if instance is None:
            await self.close()
            if code == ERROR_ACCESS_DENIED:
                raise BridgeError(
                    "PIPE_NAME_UNAVAILABLE",
                    "the Bridge pipe name is already owned by another process",
                )
            raise BridgeError(
                "IPC_UNAVAILABLE", f"the Bridge pipe name could not be claimed ({code})"
            )
        self._first_instance = instance
        for index in range(self.pool_size):
            handover = None
            if index == 0:
                # Passed in as this slot's argument: a shared field read and cleared by four
                # racing threads could leave the claimed instance unserved.
                handover, self._first_instance = self._first_instance, None
            thread = threading.Thread(
                target=self._accept_loop,
                args=(handover,),
                name=f"serverfs-bridge-pipe-accept-{index}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        deadline = self._loop.time() + _START_DEADLINE_SECONDS
        while not self._listening.is_set():
            if self._name_taken.is_set():
                await self.close()
                raise BridgeError(
                    "PIPE_NAME_UNAVAILABLE",
                    "the Bridge pipe name is already owned by another process",
                )
            if self._stop.is_set() or not any(thread.is_alive() for thread in self._threads):
                await self.close()
                raise BridgeError("IPC_UNAVAILABLE", "no Bridge pipe listener became ready")
            if self._loop.time() > deadline:
                await self.close()
                raise BridgeError("IPC_UNAVAILABLE", "the Bridge pipe did not become ready in time")
            await asyncio.sleep(0.005)

    async def serve_forever(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._stop.set()
        if self._first_instance is not None:
            # A start() that failed after claiming the name must not leave the claim behind.
            self._first_instance.close()
            self._first_instance = None
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            connection.request_close()
        loop = self._loop or asyncio.get_running_loop()
        current = threading.current_thread()
        for thread in self._threads:
            if thread is current or not thread.is_alive():
                continue
            await loop.run_in_executor(None, thread.join, _JOIN_DEADLINE_SECONDS)
        self._threads = []
        self._io.shutdown(wait=False, cancel_futures=True)

    @property
    def live_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    @property
    def listening(self) -> bool:
        return self._listening.is_set()

    # ---- accept threads ----

    def _claim_name(self) -> tuple[ServerInstance | None, int]:
        """One ``CreateNamedPipe`` with ``FILE_FLAG_FIRST_PIPE_INSTANCE``, before the pool runs."""
        return ServerInstance.create(
            self.pipe_name,
            sddl=self._dacl_sddl,
            buffer_size=_READ_CHUNK,
            first_instance=True,
        )

    def _accept_loop(self, handover: ServerInstance | None = None) -> None:
        # The name was already claimed in start(); every slot here joins the name this process
        # owns, and slot 0 is handed the instance that carried the claim flag (§7). The instance
        # arrives as an argument rather than from a shared field, because four threads racing to
        # read and clear that field could leave the claimed instance unserved.
        while not self._stop.is_set():
            if handover is not None:
                instance, code = handover, 0
                handover = None
            else:
                instance, code = ServerInstance.create(
                    self.pipe_name,
                    sddl=self._dacl_sddl,
                    buffer_size=_READ_CHUNK,
                    first_instance=False,
                )
            if instance is None:
                if code == ERROR_ACCESS_DENIED:
                    self._name_taken.set()
                    self._stop.set()
                    return
                self._stop.wait(0.05)
                continue
            connected, code = instance.begin_connect()
            if not connected:
                if code != ERROR_IO_PENDING:
                    instance.close()
                    continue
                # Readiness is measured here: an instance is now inside ConnectNamedPipe. A
                # created-but-never-connected instance was measured blocking client opens (§15).
                self._listening.set()
                if not self._await_client(instance):
                    instance.close()
                    continue
                connected, _ = instance.complete_connect()
            if not connected:
                instance.close()
                continue
            self._listening.set()
            self._serve_one(instance)

    def _await_client(self, instance: ServerInstance) -> bool:
        while not self._stop.is_set():
            if instance.wait(_ACCEPT_POLL_MS):
                return True
        instance.cancel_pending()
        instance.wait(_ACCEPT_POLL_MS * 4)
        return False

    def _serve_one(self, instance: ServerInstance) -> None:
        """Run one connection to completion on the loop, then replenish this slot."""
        assert self._loop is not None and self._on_connection is not None
        connection = PipeConnection(endpoint=self, instance=instance, executor=self._io)
        with self._connections_lock:
            self._connections.add(connection)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._on_connection(connection.reader, connection.writer), self._loop
            )
            future.result()
        except Exception:
            # The RPC core reports its own failures. A handler that dies must still free its
            # slot, or the pool would permanently lose a listener.
            pass
        finally:
            with self._connections_lock:
                self._connections.discard(connection)
            connection.dispose()
