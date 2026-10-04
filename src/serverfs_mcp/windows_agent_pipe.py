"""The Windows half of the Agent Bridge client transport (v0.11 Phase B, §4.4, §12).

ServerFS reaches the host-side Bridge over a byte Named Pipe whose name is derived from the
Bridge user's SID. The name is a rendezvous, never an authorization: before a single request
byte is written, ``BridgePipe.server_identity`` measures the process the pipe reports on its
server side and that process's token SID, and the caller asserts that SID is the identity this
own process runs as. Being able to open the pipe proves nothing (§12).

Everything here is public Win32 API with explicit ctypes signatures (§20) — the same narrow
style ``doctor.py`` already uses. Every blocking call is overlapped with its own event and a
deadline, so a stalled Bridge can neither pin the event loop nor leave a thread running past
the request timeout (§21). The framing is the frozen JSON-line contract: the read accumulates
bytes until the newline, because a byte pipe coalesces and splits writes (§16).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_NONE = 0
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OVERLAPPED = 0x40000000

ERROR_INVALID_FUNCTION = 1
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 109
ERROR_MORE_DATA = 232
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 231
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102

TOKEN_QUERY = 0x0008
TOKEN_USER = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SDDL_REVISION_1 = 1

#: Phase 0B measured these three as the conditions where another attempt inside the request
#: timeout is worth it: no instance yet, all instances busy, server side not connected.
TRANSIENT_OPEN_ERRORS = frozenset({ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY, ERROR_PIPE_NOT_CONNECTED})

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)

_KERNEL32.CreateFileW.restype = wintypes.HANDLE
_KERNEL32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_KERNEL32.CloseHandle.restype = wintypes.BOOL
_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
_KERNEL32.CreateEventW.restype = wintypes.HANDLE
_KERNEL32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_KERNEL32.SetEvent.restype = wintypes.BOOL
_KERNEL32.SetEvent.argtypes = [wintypes.HANDLE]
_KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
_KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_KERNEL32.WriteFile.restype = wintypes.BOOL
_KERNEL32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_KERNEL32.ReadFile.restype = wintypes.BOOL
_KERNEL32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_KERNEL32.GetOverlappedResult.restype = wintypes.BOOL
_KERNEL32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
]
_KERNEL32.CancelIoEx.restype = wintypes.BOOL
_KERNEL32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_KERNEL32.FormatMessageW.restype = wintypes.DWORD
_KERNEL32.FormatMessageW.argtypes = [
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_wchar_p,
    wintypes.DWORD,
    ctypes.c_void_p,
]
_KERNEL32.OpenProcess.restype = wintypes.HANDLE
_KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
_KERNEL32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
_KERNEL32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
_ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD),
]
_ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
_ADVAPI32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
_ADVAPI32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_ADVAPI32.ConvertSidToStringSidW.restype = wintypes.BOOL
_ADVAPI32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

_FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
_FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200
#: ``CreateFileW``/``CreateEventW``/``OpenProcess`` return ``c_void_p``, which ctypes hands back
#: as a Python int: ``None`` for NULL and the unsigned value for ``INVALID_HANDLE_VALUE``.
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class BridgePipeError(OSError):
    """A Win32 failure on the Bridge pipe, carrying the raw code for classification only.

    It is an ``OSError`` on purpose: the shared ``AgentBridgeClient.call`` maps an I/O failure to
    the frozen ``agent bridge is unavailable`` outcome, and the Win32 text that arrives here is a
    generic system message — never a path, SID or credential (§11).
    """

    def __init__(self, message: str, *, code: int = 0):
        super().__init__(f"{message} ({describe(code)})" if code else message)
        self.code = code


def last_error() -> int:
    return ctypes.get_last_error()


def describe(code: int) -> str:
    """Plain-English Win32 text; diagnostic only, and never a SID, path or credential."""
    if not code:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    length = _KERNEL32.FormatMessageW(
        _FORMAT_MESSAGE_FROM_SYSTEM | _FORMAT_MESSAGE_IGNORE_INSERTS,
        None,
        code,
        0,
        buffer,
        len(buffer),
        None,
    )
    if not length:
        return f"Win32 error {code}"
    return buffer.value.strip() or f"Win32 error {code}"


def sid_string(sid: int) -> str:
    text = wintypes.LPWSTR()
    if not _ADVAPI32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
        raise BridgePipeError("the pipe server SID could not be converted", code=last_error())
    try:
        return ctypes.wstring_at(text)
    finally:
        _KERNEL32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def token_user_sid(token: int) -> str:
    needed = wintypes.DWORD()
    _ADVAPI32.GetTokenInformation(ctypes.c_void_p(token), TOKEN_USER, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise BridgePipeError("the token SID could not be measured", code=last_error())
    buffer = ctypes.create_string_buffer(needed.value)
    if not _ADVAPI32.GetTokenInformation(
        ctypes.c_void_p(token), TOKEN_USER, buffer, needed, ctypes.byref(needed)
    ):
        raise BridgePipeError("the token SID could not be read", code=last_error())
    # TOKEN_USER is { SID_AND_ATTRIBUTES { PSID Sid; ULONG Attributes } }
    return sid_string(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value)


def current_user_sid() -> str:
    """This process's own token SID: the identity the measured server SID is compared to."""
    token = wintypes.HANDLE()
    if not _ADVAPI32.OpenProcessToken(
        _KERNEL32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise BridgePipeError("this process token could not be opened", code=last_error())
    try:
        return token_user_sid(int(token.value or 0))
    finally:
        _close(int(token.value or 0))


def _close(handle: int) -> None:
    if handle and handle != _INVALID_HANDLE:
        _KERNEL32.CloseHandle(ctypes.c_void_p(handle))


def security_attributes(sddl: str) -> ctypes.Structure:
    """A ``SECURITY_ATTRIBUTES`` carrying a descriptor built from ``sddl`` at creation time."""

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    descriptor = ctypes.c_void_p()
    if not _ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise BridgePipeError(
            "the Bridge pipe security descriptor could not be built", code=last_error()
        )
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = descriptor.value
    attributes.bInheritHandle = False
    return attributes


@dataclass(frozen=True)
class ServerIdentity:
    """What the pipe reports about the process on its server side (§12).

    ``user_sid`` is the authority the client asserts. ``process_id`` is measured evidence, never
    an authorization, which is why it is kept out of any client-facing output.
    """

    process_id: int
    user_sid: str

    def __repr__(self) -> str:
        return f"ServerIdentity(process_id={self.process_id}, user_sid='redacted')"


class BridgePipe:
    """One connected pipe instance with bounded, overlapped, framing-aware traffic."""

    def __init__(self, handle: int, name: str):
        self._handle = handle
        self.name = name
        self._buffer = bytearray()

    @classmethod
    def open(cls, name: str, *, deadline: float) -> BridgePipe:
        """Open with the bounded backoff §14 allows; every other failure is immediate."""
        delay = 0.005
        last_code = 0
        while True:
            handle = _KERNEL32.CreateFileW(
                name,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_NONE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
                None,
            )
            if handle not in (None, 0, _INVALID_HANDLE):
                return cls(int(handle), name)
            code = last_error()
            last_code = code
            if code not in TRANSIENT_OPEN_ERRORS or time.monotonic() >= deadline:
                raise BridgePipeError("the Bridge pipe could not be opened", code=last_code)
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)

    @property
    def handle(self) -> int:
        return self._handle

    def server_identity(self) -> ServerIdentity:
        pid = wintypes.DWORD()
        if not _KERNEL32.GetNamedPipeServerProcessId(
            ctypes.c_void_p(self._handle), ctypes.byref(pid)
        ):
            raise BridgePipeError(
                "the Bridge server process could not be measured", code=last_error()
            )
        process = _KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if process in (None, 0, _INVALID_HANDLE):
            raise BridgePipeError(
                "the Bridge server process could not be opened", code=last_error()
            )
        token = wintypes.HANDLE()
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION is enough to read another process's token,
            # which is what Phase 0B measured on this machine.
            if not _ADVAPI32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
                raise BridgePipeError(
                    "the Bridge server token could not be opened", code=last_error()
                )
            try:
                return ServerIdentity(int(pid.value), token_user_sid(int(token.value or 0)))
            finally:
                _close(int(token.value or 0))
        finally:
            _close(int(process))

    def write_all(self, data: bytes, *, deadline: float) -> None:
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data, len(data))
        event, overlapped = self._arm()
        try:
            ok = _KERNEL32.WriteFile(
                ctypes.c_void_p(self._handle),
                buffer,
                len(data),
                ctypes.byref(written),
                ctypes.byref(overlapped),
            )
            code = 0 if ok else last_error()
            if not ok and code != ERROR_IO_PENDING:
                raise BridgePipeError("the Bridge request could not be written", code=code)
            if not ok:
                self._settle(event, overlapped, deadline=deadline)
                ok = _KERNEL32.GetOverlappedResult(
                    ctypes.c_void_p(self._handle),
                    ctypes.byref(overlapped),
                    ctypes.byref(written),
                    False,
                )
                if not ok:
                    raise BridgePipeError(
                        "the Bridge request could not be written", code=last_error()
                    )
            if int(written.value) != len(data):
                raise BridgePipeError("the Bridge request was only partly written")
        finally:
            _close(event)

    def read_line(self, *, limit: int, deadline: float) -> bytes:
        """Accumulate until the newline; ``b""`` means the server ended the stream."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return line
            if len(self._buffer) > limit:
                self._buffer.clear()
                raise ValueError("agent bridge response exceeds configured limit")
            data = self._read_once(deadline=deadline)
            if data is None:
                return bytes(self._buffer)
            self._buffer.extend(data)

    def _read_once(self, *, deadline: float) -> bytes | None:
        """One bounded read; ``None`` when the server closed its end."""
        chunk = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD()
        event, overlapped = self._arm()
        try:
            ok = _KERNEL32.ReadFile(
                ctypes.c_void_p(self._handle),
                chunk,
                65536,
                ctypes.byref(got),
                ctypes.byref(overlapped),
            )
            code = 0 if ok else last_error()
            if not ok and code not in (ERROR_IO_PENDING, ERROR_MORE_DATA):
                if code in (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED):
                    return None
                raise BridgePipeError("the Bridge response could not be read", code=code)
            if ok:
                return chunk.raw[: int(got.value)] or None
            self._settle(event, overlapped, deadline=deadline)
            result = wintypes.DWORD()
            if not _KERNEL32.GetOverlappedResult(
                ctypes.c_void_p(self._handle),
                ctypes.byref(overlapped),
                ctypes.byref(result),
                False,
            ):
                error = last_error()
                if error in (ERROR_BROKEN_PIPE, ERROR_NO_DATA):
                    return None
                raise BridgePipeError("the Bridge response could not be read", code=error)
            return chunk.raw[: int(result.value)] or None
        finally:
            _close(event)

    def close(self) -> None:
        if self._handle:
            _close(self._handle)
            self._handle = 0

    def _arm(self) -> tuple[int, Overlapped]:
        event = _KERNEL32.CreateEventW(None, True, False, None)
        if event in (None, 0, _INVALID_HANDLE):
            raise BridgePipeError("a pipe wait event could not be created", code=last_error())
        overlapped = Overlapped()
        overlapped.hEvent = ctypes.c_void_p(int(event))
        return int(event), overlapped

    def _settle(self, event: int, overlapped: Overlapped, *, deadline: float) -> None:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        waited = _KERNEL32.WaitForSingleObject(ctypes.c_void_p(event), remaining_ms)
        if waited == WAIT_TIMEOUT:
            _KERNEL32.CancelIoEx(ctypes.c_void_p(self._handle), ctypes.byref(overlapped))
            _KERNEL32.WaitForSingleObject(ctypes.c_void_p(event), 1000)
            raise BridgePipeError("the Bridge pipe request timed out", code=ERROR_OPERATION_ABORTED)
        if waited != WAIT_OBJECT_0:
            raise BridgePipeError("the Bridge pipe wait failed", code=last_error())
