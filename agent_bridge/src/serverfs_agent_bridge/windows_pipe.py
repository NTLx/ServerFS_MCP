"""Windows named-pipe and peer-identity primitives behind the §3 local-IPC seam.

Narrow ctypes usage in the style of ``serverfs_mcp.doctor``: public kernel32/advapi32 entry
points with explicit ``restype``/``argtypes``, and an error surface that keeps the Win32 code
so the caller can decide which codes are transient. Phase 0B measured every behaviour assumed
here. Four choices are deliberate:

- ``CancelSynchronousIo`` is never used. Measured on WorkPC: a thread handle opened with the
  rights an ordinary process can obtain for its own thread is refused (``ERROR_ACCESS_DENIED``)
  and the blocked reader stayed blocked. Bounded I/O is an overlapped operation with a timed
  ``WaitForSingleObject`` plus ``CancelIoEx`` on the pipe handle instead, in the order measured
  working: cancel, wait for the completion to be observed, then ``GetOverlappedResult`` reports
  ``ERROR_OPERATION_ABORTED``.
- An instance is created with ``FILE_FLAG_OVERLAPPED``, so connect, read *and* write each carry
  an ``OVERLAPPED`` — a synchronous ``WriteFile`` on such a handle is invalid. Each in-flight
  operation's ``OVERLAPPED`` is kept alive on the instance until it is resolved, because
  ``GetOverlappedResult`` must be given the very structure the pending call used.
- ``PIPE_UNLIMITED_INSTANCES`` is the ceiling on the *name*, not on Bridge concurrency: live
  connections are bounded by the accept pool, since a slot creates its next instance only after
  its current connection has finished.
- The SACL is never requested. Asking for it fails with ``ERROR_PRIVILEGE_NOT_HELD`` from an
  unelevated process (measured), and the Bridge has no use for it.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from .errors import BridgeError
from .windows_security import (
    INVALID_HANDLE_VALUE,
    KERNEL32,
    SecurityAttributes,
    last_error,
    security_attributes,
    sid_to_string,
)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_SHARE_NONE = 0

ERROR_INVALID_FUNCTION = 1
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 109
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 231
ERROR_PIPE_CONNECTED = 535
ERROR_CANT_IMPERSONATE_NAMED_PIPE = 1368

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

TOKEN_QUERY = 0x0008
TokenUser = 1
TokenImpersonationLevel = 9
TokenType = 8
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_CURRENT_THREAD = -2  # GetCurrentThread() pseudo-handle
_POLL_SLICE_MS = 25
_CANCEL_SETTLE_MS = 1000

#: Client-side connect codes that may be retried inside the caller's own timeout (§14).
TRANSIENT_CONNECT_ERRORS = frozenset(
    {ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA}
)

#: Codes that mean "the peer is gone", not "the Bridge failed".
_PIPE_END_CODES = frozenset(
    {
        ERROR_BROKEN_PIPE,
        ERROR_NO_DATA,
        ERROR_PIPE_NOT_CONNECTED,
        ERROR_OPERATION_ABORTED,
        ERROR_INVALID_HANDLE,
        ERROR_INVALID_FUNCTION,
    }
)

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _declare() -> None:
    for name, restype, argtypes in [
        (
            "CreateNamedPipeW",
            wintypes.HANDLE,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(SecurityAttributes),
            ],
        ),
        ("ConnectNamedPipe", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(Overlapped)]),
        ("DisconnectNamedPipe", wintypes.BOOL, [wintypes.HANDLE]),
        (
            "ReadFile",
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(Overlapped),
            ],
        ),
        (
            "WriteFile",
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(Overlapped),
            ],
        ),
        (
            "GetOverlappedResult",
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                ctypes.POINTER(Overlapped),
                ctypes.POINTER(wintypes.DWORD),
                wintypes.BOOL,
            ],
        ),
        ("CancelIoEx", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(Overlapped)]),
        (
            "CreateEventW",
            wintypes.HANDLE,
            [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
        ),
        ("ResetEvent", wintypes.BOOL, [wintypes.HANDLE]),
        ("WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        (
            "CreateFileW",
            wintypes.HANDLE,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(SecurityAttributes),
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ],
        ),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
        (
            "GetNamedPipeClientProcessId",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        ),
        (
            "GetNamedPipeClientSessionId",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        ),
        (
            "GetNamedPipeServerProcessId",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        ),
        (
            "GetNamedPipeServerSessionId",
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        ),
        ("OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
    ]:
        function = getattr(KERNEL32, name)
        function.restype = restype
        function.argtypes = argtypes
    KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    for name, restype, argtypes in [
        ("ImpersonateNamedPipeClient", wintypes.BOOL, [wintypes.HANDLE]),
        ("RevertToSelf", wintypes.BOOL, []),
        (
            "OpenThreadToken",
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)],
        ),
        (
            "OpenProcessToken",
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
        ),
        (
            "GetTokenInformation",
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ],
        ),
    ]:
        function = getattr(advapi32, name)
        function.restype = restype
        function.argtypes = argtypes


_declare()


def close_handle(handle: int | None) -> None:
    if not handle or handle == INVALID_HANDLE_VALUE:
        return
    KERNEL32.CloseHandle(ctypes.c_void_p(int(handle)))


def create_event() -> int:
    handle = KERNEL32.CreateEventW(None, True, False, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise BridgeError("IPC_UNAVAILABLE", "a pipe wait event could not be created")
    return int(handle)


def wait(handle: int, timeout_ms: int) -> int:
    return int(KERNEL32.WaitForSingleObject(ctypes.c_void_p(int(handle)), int(timeout_ms)))


def token_user_sid(token: int) -> tuple[str | None, int]:
    """The TokenUser SID of an open token handle, as a canonical ``S-1-…`` string."""
    needed = wintypes.DWORD()
    advapi32.GetTokenInformation(ctypes.c_void_p(token), TokenUser, None, 0, ctypes.byref(needed))
    if not needed.value:
        return None, last_error()
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(
        ctypes.c_void_p(token), TokenUser, buffer, needed, ctypes.byref(needed)
    ):
        return None, last_error()
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
    text = sid_to_string(sid)
    return text, 0 if text else last_error()


def token_dword(token: int, information_class: int) -> int | None:
    needed = wintypes.DWORD()
    advapi32.GetTokenInformation(
        ctypes.c_void_p(token), information_class, None, 0, ctypes.byref(needed)
    )
    if not needed.value:
        return None
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(
        ctypes.c_void_p(token), information_class, buffer, needed, ctypes.byref(needed)
    ):
        return None
    return int(ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value)


def process_user_sid(pid: int) -> tuple[str | None, int]:
    """The TokenUser SID of ``pid`` (Phase 0B ID-REVERSE: measured working)."""
    process = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if process in (None, INVALID_HANDLE_VALUE):
        return None, last_error()
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return None, last_error()
        return token_user_sid(int(token.value or 0))
    finally:
        close_handle(int(token.value or 0))
        close_handle(int(process))


def pipe_peer_ids(handle: int, *, server_side: bool) -> tuple[int, int, int]:
    """``(pid, session_id, error)`` for the other end of a connected pipe."""
    pid = wintypes.DWORD()
    session = wintypes.DWORD()
    if server_side:
        got_pid = KERNEL32.GetNamedPipeServerProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
        got_session = KERNEL32.GetNamedPipeServerSessionId(
            ctypes.c_void_p(handle), ctypes.byref(session)
        )
    else:
        got_pid = KERNEL32.GetNamedPipeClientProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
        got_session = KERNEL32.GetNamedPipeClientSessionId(
            ctypes.c_void_p(handle), ctypes.byref(session)
        )
    if not got_pid:
        return 0, 0, last_error()
    if not got_session:
        return int(pid.value), 0, last_error()
    return int(pid.value), int(session.value), 0


class PeerIdentity:
    """Identity of the process connected to one pipe instance (§11).

    ``sid`` is the authorization field; ``process_id`` and ``session_id`` are measured
    diagnostics. The SID stays out of ``repr`` so identity material cannot reach a log line.
    """

    __slots__ = ("sid", "process_id", "session_id", "impersonation_level", "token_type")

    def __init__(self, sid, process_id, session_id, impersonation_level, token_type):
        self.sid = sid
        self.process_id = process_id
        self.session_id = session_id
        self.impersonation_level = impersonation_level
        self.token_type = token_type

    def __repr__(self) -> str:
        return (
            f"PeerIdentity(pid={self.process_id}, session={self.session_id}, "
            f"impersonation_level={self.impersonation_level}, token_type={self.token_type})"
        )


class ServerInstance:
    """One server pipe instance, its connect wait, and the framing operations on it.

    An instance serves exactly one connection and is then closed and replaced — the
    replenishing-pool shape Phase 0B measured — so a stalled peer cannot affect any other
    connection and the pool size is the bound on live work.
    """

    def __init__(self, handle: int, event: int, name: str):
        self.handle = handle
        self.event = event
        self.name = name
        self._in_flight: list[Overlapped] = []

    @classmethod
    def create(
        cls,
        name: str,
        *,
        sddl: str,
        buffer_size: int,
        first_instance: bool,
    ) -> tuple[ServerInstance | None, int]:
        """``(instance, 0)`` or ``(None, win32_error)``; the client wait is not armed yet."""
        attributes = security_attributes(sddl)
        event = create_event()
        open_mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
        if first_instance:
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = KERNEL32.CreateNamedPipeW(
            name,
            open_mode,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,
            buffer_size,
            buffer_size,
            0,
            ctypes.byref(attributes),
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            code = last_error()
            close_handle(event)
            return None, code
        return cls(int(handle), event, name), 0

    # ---- connect (§15: readiness means a real listener inside ConnectNamedPipe) ----

    def begin_connect(self) -> tuple[bool, int]:
        """``(True, 0)`` when a client is already here, ``(False, ERROR_IO_PENDING)`` once the
        wait is armed, ``(False, code)`` when it cannot be."""
        overlapped = self._arm()
        if KERNEL32.ConnectNamedPipe(ctypes.c_void_p(self.handle), ctypes.byref(overlapped)):
            self._settle(overlapped)
            return True, 0
        code = last_error()
        if code == ERROR_PIPE_CONNECTED:
            self._settle(overlapped)
            return True, 0
        if code != ERROR_IO_PENDING:
            self._settle(overlapped)
        return False, code

    def complete_connect(self) -> tuple[bool, int]:
        """Resolve the armed connect after its event fired."""
        overlapped = self._oldest()
        if overlapped is None:
            return True, 0
        got = wintypes.DWORD()
        ok = KERNEL32.GetOverlappedResult(
            ctypes.c_void_p(self.handle), ctypes.byref(overlapped), ctypes.byref(got), True
        )
        code = 0 if ok else last_error()
        self._settle(overlapped)
        if ok or code == ERROR_PIPE_CONNECTED:
            return True, 0
        return False, code

    # ---- framing ----

    def read(self, size: int, *, timeout_ms: int, abort: threading.Event) -> tuple[bytes, str]:
        """One bounded overlapped read: ``"data"``, ``"eof"``, ``"idle"``, ``"closed"`` or
        ``"error:<code>"``. §18: ``idle`` must end the connection, not pin the instance."""
        buffer = ctypes.create_string_buffer(size)
        overlapped = self._arm()
        got = wintypes.DWORD()
        if KERNEL32.ReadFile(
            ctypes.c_void_p(self.handle), buffer, size, ctypes.byref(got), ctypes.byref(overlapped)
        ):
            data = buffer.raw[: got.value]
            self._settle(overlapped)
            return data, "data"
        code = last_error()
        if code != ERROR_IO_PENDING:
            self._settle(overlapped)
            return (b"", "eof") if code in _PIPE_END_CODES else (b"", f"error:{code}")
        if not self._wait(timeout_ms, abort):
            self._cancel(overlapped)
            self._settle(overlapped)
            return (b"", "closed") if abort.is_set() else (b"", "idle")
        count = self._collect(overlapped)
        self._settle(overlapped)
        if count is None:
            code = last_error()
            return (b"", "eof") if code in _PIPE_END_CODES else (b"", f"error:{code}")
        return buffer.raw[:count], "data"

    def write(self, data: bytes, *, timeout_ms: int, abort: threading.Event) -> str:
        """One bounded overlapped write; ``"ok"`` or the reason it did not complete."""
        if not data:
            return "ok"
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data, len(data))
        overlapped = self._arm()
        if KERNEL32.WriteFile(
            ctypes.c_void_p(self.handle),
            buffer,
            len(data),
            ctypes.byref(written),
            ctypes.byref(overlapped),
        ):
            self._settle(overlapped)
            return "ok"
        code = last_error()
        if code != ERROR_IO_PENDING:
            self._settle(overlapped)
            return "eof" if code in _PIPE_END_CODES else f"error:{code}"
        if not self._wait(timeout_ms, abort):
            self._cancel(overlapped)
            self._settle(overlapped)
            return "closed" if abort.is_set() else "timeout"
        count = self._collect(overlapped)
        self._settle(overlapped)
        return "ok" if count is not None else "eof"

    # ---- identity ----

    def peer_ids(self) -> tuple[int, int, int]:
        return pipe_peer_ids(self.handle, server_side=False)

    def measure_peer(self) -> PeerIdentity:
        return measure_client_identity(self.handle)

    # ---- lifecycle ----

    def wait(self, timeout_ms: int) -> bool:
        """True when the armed operation completed within the timeout."""
        return wait(self.event, timeout_ms) == WAIT_OBJECT_0

    def cancel_pending(self) -> None:
        KERNEL32.CancelIoEx(ctypes.c_void_p(self.handle), None)

    def close(self) -> None:
        self.cancel_pending()
        close_handle(self.handle)
        close_handle(self.event)
        self._in_flight.clear()

    # ---- overlapped bookkeeping ----

    def _arm(self) -> Overlapped:
        overlapped = Overlapped()
        overlapped.hEvent = ctypes.c_void_p(self.event)
        KERNEL32.ResetEvent(ctypes.c_void_p(self.event))
        self._in_flight.append(overlapped)
        return overlapped

    def _oldest(self) -> Overlapped | None:
        return self._in_flight[0] if self._in_flight else None

    def _settle(self, overlapped: Overlapped) -> None:
        if overlapped in self._in_flight:
            self._in_flight.remove(overlapped)

    def _wait(self, timeout_ms: int, abort: threading.Event) -> bool:
        waited = 0
        while waited < timeout_ms:
            if abort.is_set():
                return False
            slice_ms = min(_POLL_SLICE_MS, max(1, timeout_ms - waited))
            if wait(self.event, slice_ms) == WAIT_OBJECT_0:
                return True
            waited += slice_ms
        return False

    def _cancel(self, overlapped: Overlapped) -> None:
        KERNEL32.CancelIoEx(ctypes.c_void_p(self.handle), ctypes.byref(overlapped))
        # The cancelled operation must be observed as complete before its result is read —
        # cancel, wait, then GetOverlappedResult reports ERROR_OPERATION_ABORTED (measured).
        wait(self.event, _CANCEL_SETTLE_MS)

    def _collect(self, overlapped: Overlapped) -> int | None:
        got = wintypes.DWORD()
        if KERNEL32.GetOverlappedResult(
            ctypes.c_void_p(self.handle), ctypes.byref(overlapped), ctypes.byref(got), True
        ):
            return int(got.value)
        return None


def measure_client_identity(handle: int) -> PeerIdentity:
    """Measure the connected client after at least one completed read on this instance.

    ``RevertToSelf`` runs in ``finally`` on every path — success, query failure and assertion
    failure alike — so a worker thread can never continue impersonated (§10).
    """
    process_id, session_id, _ = pipe_peer_ids(handle, server_side=False)
    if not advapi32.ImpersonateNamedPipeClient(ctypes.c_void_p(handle)):
        raise BridgeError(
            "PEER_IDENTITY_UNAVAILABLE",
            f"the pipe client could not be impersonated (Win32 error {last_error()})",
        )
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenThreadToken(_CURRENT_THREAD, TOKEN_QUERY, False, ctypes.byref(token)):
            raise BridgeError(
                "PEER_IDENTITY_UNAVAILABLE",
                f"the impersonated client token could not be opened (Win32 error {last_error()})",
            )
        try:
            token_handle = int(token.value or 0)
            sid, error = token_user_sid(token_handle)
            if sid is None:
                raise BridgeError(
                    "PEER_IDENTITY_UNAVAILABLE",
                    f"the impersonated client SID could not be read (Win32 error {error})",
                )
            return PeerIdentity(
                sid=sid,
                process_id=process_id,
                session_id=session_id,
                impersonation_level=token_dword(token_handle, TokenImpersonationLevel) or 0,
                token_type=token_dword(token_handle, TokenType) or 0,
            )
        finally:
            close_handle(int(token.value or 0))
    finally:
        if not advapi32.RevertToSelf():
            raise BridgeError(
                "PEER_IDENTITY_UNAVAILABLE",
                f"impersonation could not be reverted (Win32 error {last_error()})",
            )


def thread_is_impersonating() -> bool:
    """True when the current thread still holds an impersonation token."""
    token = wintypes.HANDLE()
    if not advapi32.OpenThreadToken(_CURRENT_THREAD, TOKEN_QUERY, False, ctypes.byref(token)):
        return False
    close_handle(int(token.value or 0))
    return True


def open_client(name: str) -> tuple[int, int]:
    """Client-side open of one instance: ``(handle, 0)`` or ``(0, win32_error)``."""
    handle = KERNEL32.CreateFileW(
        name,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_NONE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        return 0, last_error()
    return int(handle), 0


def client_write(handle: int, data: bytes) -> int:
    written = wintypes.DWORD()
    buffer = ctypes.create_string_buffer(data, len(data))
    if KERNEL32.WriteFile(ctypes.c_void_p(handle), buffer, len(data), ctypes.byref(written), None):
        return 0
    return last_error()


def client_read(handle: int, size: int) -> tuple[bytes, int]:
    buffer = ctypes.create_string_buffer(size)
    got = wintypes.DWORD()
    if KERNEL32.ReadFile(ctypes.c_void_p(handle), buffer, size, ctypes.byref(got), None):
        return buffer.raw[: got.value], 0
    return b"", last_error()
