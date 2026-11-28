"""Windows IPv4 TCP listener ownership proof for independent HTTP Core.

Microsoft GetExtendedTcpTable / TCP_TABLE_OWNER_PID_LISTENER is the narrow
authoritative socket-owner surface. A 401 HTTP response or PID alone is never
a process-generation proof. No process is started or terminated here.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

_AF_INET = 2
_TCP_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
_TCP_LISTEN = 2
_MAX_TABLE_BYTES = 4 * 1024 * 1024
_ROW_BYTES = 24


class ListenerProofError(Exception):
    """No credentials, private filesystem paths or raw OS strings in errors."""


@dataclass(frozen=True)
class ListenerRow:
    address: str
    port: int
    pid: int


@dataclass(frozen=True)
class CoreListenerProof:
    pid: int
    creation_time: int
    image_path: Path
    port: int
    address: str = "127.0.0.1"


def _parse_ipv4_listeners(payload: bytes) -> tuple[ListenerRow, ...]:
    """Parse MIB_TCPTABLE_OWNER_PID; DWORD address/port are network-byte data.

    MIB_TCPROW_OWNER_PID consists of exactly six 32-bit DWORDs, while the
    row bytes begin after dwNumEntries. All fixed-width bounds are checked
    before reading, including integer overflow / corrupt table counts.
    """
    if len(payload) < 4:
        raise ListenerProofError("Windows TCP owner table is truncated")
    entries = struct.unpack_from("<I", payload)[0]
    if entries > (len(payload) - 4) // _ROW_BYTES:
        raise ListenerProofError("Windows TCP owner table length is invalid")
    rows: list[ListenerRow] = []
    for i in range(entries):
        state, addr, local_port, _remote_addr, _remote_port, pid = struct.unpack_from(
            "<6I", payload, 4 + i * _ROW_BYTES
        )
        if state != _TCP_LISTEN:
            continue
        address = socket.inet_ntoa(addr.to_bytes(4, "little"))
        port = int.from_bytes(local_port.to_bytes(4, "little")[:2], "big")
        rows.append(ListenerRow(address=address, port=port, pid=pid))
    return tuple(rows)


def _windows_ipv4_tcp_listeners() -> tuple[ListenerRow, ...]:
    if sys.platform != "win32" or os.name != "nt":
        raise ListenerProofError("Windows socket ownership requires Windows")
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    query = iphlpapi.GetExtendedTcpTable
    query.restype = wintypes.DWORD
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    needed = wintypes.DWORD(0)
    result = query(None, ctypes.byref(needed), False, _AF_INET, _TCP_OWNER_PID_LISTENER, 0)
    if result not in (0, _ERROR_INSUFFICIENT_BUFFER):
        raise ListenerProofError("Windows TCP owner table query is unavailable")
    if needed.value < 4 or needed.value > _MAX_TABLE_BYTES:
        raise ListenerProofError("Windows TCP owner table size is outside bounds")
    # A concurrent socket-open may grow the table. Retry only within a fixed
    # byte/time budget; never accept a truncated, partially filled table.
    for _ in range(3):
        capacity = needed.value
        if capacity > _MAX_TABLE_BYTES:
            break
        buffer = ctypes.create_string_buffer(capacity)
        needed = wintypes.DWORD(capacity)
        result = query(
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(needed),
            False,
            _AF_INET,
            _TCP_OWNER_PID_LISTENER,
            0,
        )
        if result == 0:
            if needed.value > capacity:
                raise ListenerProofError("Windows TCP owner table exceeded its buffer")
            return _parse_ipv4_listeners(buffer.raw[: needed.value])
        if result != _ERROR_INSUFFICIENT_BUFFER:
            raise ListenerProofError("Windows TCP owner snapshot was rejected")
    raise ListenerProofError("Windows TCP owner table changed repeatedly")


def prove_core_listener(
    *,
    expected_port: int,
    expected_hostname: str | None = None,
    observed_rows: tuple[ListenerRow, ...] | None = None,
) -> CoreListenerProof:
    """Prove exact registered Core + process generation + loopback listener.

    A caller may inject rows for a *pure test*. Live calls always query
    Microsoft's process-owned listening TCP table, not HTTP output.
    """
    if type(expected_port) is not int or not 1 <= expected_port <= 65535:
        raise ListenerProofError("Core owner port is invalid")
    from . import windows_core_control as owner
    from .windows_process import (
        ProcessIdentity,
        registered_pythonw_image_matches,
        same_process,
    )

    try:
        manifest, registered = owner._require_owned()
        state = owner._read_state()
    except Exception as exc:  # noqa: BLE001 - treat any inaccessible proof as refusal
        raise ListenerProofError("Core registered owner proof is unavailable") from exc
    if not registered or manifest["port"] != expected_port:
        raise ListenerProofError("Core registered owner targets a different port")
    if expected_hostname is not None and manifest["hostname"] != expected_hostname:
        raise ListenerProofError("Core registered owner targets a different public Host")
    pid = state.get("core_pid")
    creation = state.get("core_creation_time")
    image = state.get("core_image_path")
    if (
        state.get("component") != "mcp-core"
        or state.get("stage") != "owner-started"
        or type(pid) is not int
        or pid <= 0
        or type(creation) is not int
        or creation <= 0
        or not isinstance(image, str)
        or not image
    ):
        raise ListenerProofError("Core running generation is not verified")
    if not registered_pythonw_image_matches(
        registered_launcher=Path(manifest["pythonw"]),
        kernel_image=Path(image),
        venv_prefix=Path(sys.prefix),
        current_executable=Path(sys.executable),
        base_executable=(
            Path(sys._base_executable)
            if isinstance(getattr(sys, "_base_executable", None), str)
            else None
        ),
        require_pythonw_entrypoint=False,
        permit_peer_controller=True,
    ):
        raise ListenerProofError("Core running generation image is not attested")
    identity = ProcessIdentity(pid=pid, image_path=Path(image), creation_time=creation)
    if not same_process(identity):
        raise ListenerProofError("Core recorded generation is not alive")
    rows = _windows_ipv4_tcp_listeners() if observed_rows is None else observed_rows
    bound = tuple(row for row in rows if row.port == expected_port)
    if len(bound) != 1 or bound[0].address != "127.0.0.1" or bound[0].pid != pid:
        raise ListenerProofError("Core loopback listener does not belong to its generation")
    # Check again after the kernel TCP table snapshot to narrow the PID-reuse
    # race. The Core's own single-owner mutex prevents same-user duplicates.
    if not same_process(identity):
        raise ListenerProofError("Core owner changed during listener verification")
    return CoreListenerProof(
        pid=pid,
        creation_time=creation,
        image_path=identity.image_path,
        port=expected_port,
    )
