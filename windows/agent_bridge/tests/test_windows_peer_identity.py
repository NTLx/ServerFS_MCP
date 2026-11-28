"""Windows peer-identity tests — §9 ordering, §10 revert, §11 and §12 measurement.

These drive one ``ServerInstance`` and one client handle directly, on the calling thread, so the
impersonation lifecycle is observed where it happens: the assertion that a thread is no longer
impersonated is made in that same thread, not inferred from a wrapper.
"""

from __future__ import annotations

import os
import threading

import pytest

from pipe_support import PipeClient, new_pipe_name
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.windows_pipe import (
    ERROR_CANT_IMPERSONATE_NAMED_PIPE,
    ERROR_IO_PENDING,
    ServerInstance,
    measure_client_identity,
    pipe_peer_ids,
    process_user_sid,
    thread_is_impersonating,
)
from serverfs_agent_bridge.windows_security import current_user_sid, pipe_dacl_sddl

require_windows_kernel("peer identity is an impersonated Named Pipe client SID")


class Connected:
    """One server instance with one client attached, cleaned up on exit."""

    def __init__(self, sddl: str | None = None):
        self.name = new_pipe_name()
        self.instance, error = ServerInstance.create(
            self.name,
            sddl=sddl or pipe_dacl_sddl(current_user_sid()),
            buffer_size=8192,
            first_instance=True,
        )
        assert self.instance is not None, f"instance creation failed with {error}"
        self.connected, code = self.instance.begin_connect()
        if not self.connected:
            assert code == ERROR_IO_PENDING, f"ConnectNamedPipe failed with {code}"
        self.client = PipeClient(self.name)
        self.client.connect()
        if not self.connected:
            assert self.instance.wait(3000)
            self.connected, _ = self.instance.complete_connect()
        assert self.connected

    def write_from_client(self, payload: bytes) -> None:
        self.client.write(payload)

    def read_on_server(self, size: int = 8192) -> tuple[bytes, str]:
        return self.instance.read(size, timeout_ms=3000, abort=threading.Event())

    def close(self) -> None:
        self.client.close()
        self.instance.close()


def test_impersonation_is_impossible_before_a_completed_read() -> None:
    """§9: Phase 0B measured ERROR_CANT_IMPERSONATE_NAMED_PIPE here; the order is not optional."""
    attached = Connected()
    try:
        with pytest.raises(BridgeError) as raised:
            attached.instance.measure_peer()
        assert raised.value.code == "PEER_IDENTITY_UNAVAILABLE"
        assert str(ERROR_CANT_IMPERSONATE_NAMED_PIPE) in raised.value.message
    finally:
        attached.close()


def test_identity_is_measured_after_the_first_completed_read() -> None:
    attached = Connected()
    try:
        attached.write_from_client(b"ping\n")
        data, why = attached.read_on_server()
        assert why == "data" and data == b"ping\n"
        identity = attached.instance.measure_peer()
        assert identity.sid == current_user_sid()
        assert identity.process_id == os.getpid()
        # Measured here: the thread holds a TokenImpersonation (2) at SecurityImpersonation (2),
        # which is the level that still allows the SID comparison and nothing more.
        assert identity.token_type == 2
        assert identity.impersonation_level == 2
    finally:
        attached.close()


def test_revert_to_self_runs_on_the_success_path() -> None:
    attached = Connected()
    try:
        attached.write_from_client(b"ping\n")
        attached.read_on_server()
        # Same thread as the measurement below, so this is the thread that was impersonated.
        identity = measure_client_identity(attached.instance.handle)
        assert identity.sid == current_user_sid()
        assert thread_is_impersonating() is False
    finally:
        attached.close()


def test_revert_to_self_runs_on_the_failure_path() -> None:
    attached = Connected()
    try:
        with pytest.raises(BridgeError):
            measure_client_identity(attached.instance.handle)
        assert thread_is_impersonating() is False
    finally:
        attached.close()


def test_revert_to_self_runs_when_the_caller_raises_after_measuring() -> None:
    """§10: an authorization failure raised by the caller still leaves no impersonation behind."""
    attached = Connected()
    try:
        attached.write_from_client(b"ping\n")
        attached.read_on_server()

        def assert_and_fail(sid: str) -> None:
            # The production path asserts inside this window; the finally in
            # measure_client_identity is what has to hold even when the caller raises.
            identity = measure_client_identity(attached.instance.handle)
            assert identity.sid == sid

        with pytest.raises(AssertionError):
            assert_and_fail("S-1-5-21-0-0-0-9999")
        assert thread_is_impersonating() is False
    finally:
        attached.close()


def test_pid_and_session_are_measured_evidence_not_authority() -> None:
    attached = Connected()
    try:
        attached.write_from_client(b"ping\n")
        attached.read_on_server()
        client_pid, client_session, error = attached.instance.peer_ids()
        assert error == 0
        server_pid, server_session, _ = pipe_peer_ids(attached.client.handle, server_side=True)
        # One process owns both ends here, so the two directions must agree; the E2E driver
        # proves the same pair across two processes.
        assert client_pid == server_pid == os.getpid()
        assert client_session == server_session
        identity = attached.instance.measure_peer()
        assert (identity.process_id, identity.session_id) == (client_pid, client_session)
    finally:
        attached.close()


def test_the_server_side_sid_is_readable_from_the_client() -> None:
    """§12: the client can measure who owns the pipe, so it never has to trust the name."""
    attached = Connected()
    try:
        server_pid, _session, error = pipe_peer_ids(attached.client.handle, server_side=True)
        assert error == 0
        sid, lookup_error = process_user_sid(server_pid)
        assert sid == current_user_sid(), lookup_error
    finally:
        attached.close()
