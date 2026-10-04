"""Windows Named-Pipe RPC tests — the §36 matrix for the local-IPC and peer-identity seams.

Every case here drives a real ``BridgeProtocolServer`` over a real Named Pipe with real
``WriteFile``/``ReadFile`` traffic, so the framing, size bounds, error vocabulary and
authorization asserted below are the frozen contract as a Windows client sees it.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from pathlib import Path

import pytest

from pipe_support import (
    PipeClient,
    RunningBridge,
    make_service,
    new_pipe_name,
    padded_request_frame,
    request_frame,
    wait_until,
)
from platform_contract import require_windows_kernel
from serverfs_agent_bridge.protocol import MAX_REQUEST_BYTES, PIPE_IDLE_TIMEOUT_SECONDS
from serverfs_agent_bridge.windows_pipe import (
    ERROR_IO_PENDING,
    ServerInstance,
    close_handle,
    open_client,
)
from serverfs_agent_bridge.windows_security import current_user_sid, pipe_dacl_sddl

require_windows_kernel("the local-IPC seam is a Named Pipe and peer identity is a client SID")

FOREIGN_SID = "S-1-5-21-1-1-1-4242"


def bridge(tmp_path: Path, **kwargs: object) -> RunningBridge:
    return RunningBridge(make_service(tmp_path), **kwargs)


def test_single_request_round_trip(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        response = client.request("runtime.list", {})
    assert response["ok"] is True
    assert response["result"]["runtimes"][0]["name"] == "fake"


def test_sequential_clients_reuse_the_pool(tmp_path: Path) -> None:
    with bridge(tmp_path) as server:
        for _ in range(5):
            with PipeClient(server.pipe_name) as client:
                assert client.request("runtime.list", {})["ok"] is True
        assert wait_until(lambda: server.server._pipe.live_connections == 0)


def test_concurrent_clients_are_served_by_distinct_instances(tmp_path: Path) -> None:
    with bridge(tmp_path) as server:
        workers = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:

            def call(_: int) -> bool:
                with PipeClient(server.pipe_name) as client:
                    return client.request("runtime.list", {})["ok"] is True

            results = list(workers.map(call, range(4)))
        finally:
            workers.shutdown(wait=True)
        assert results == [True] * 4
        assert wait_until(lambda: server.server._pipe.live_connections == 0)


def test_pool_size_bounds_simultaneous_connections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("serverfs_agent_bridge.protocol.PIPE_POOL_SIZE", 2)
    with bridge(tmp_path) as server:
        held = [PipeClient(server.pipe_name) for _ in range(2)]
        for client in held:
            client.connect()
            assert client.request("runtime.list", {})["ok"] is True
        saturated = PipeClient(server.pipe_name, deadline_seconds=1.0)
        started = time.monotonic()
        with pytest.raises(ConnectionError) as raised:
            saturated.connect()
        assert "231" in str(raised.value) or "109" in str(raised.value)
        assert time.monotonic() - started < 5.0, "the connect retry loop is not bounded"
        held[0].close()
        assert wait_until(lambda: server.server._pipe.live_connections <= 1)
        with PipeClient(server.pipe_name) as replacement:
            assert replacement.request("runtime.list", {})["ok"] is True
        for client in held[1:]:
            client.close()


def test_instance_pool_is_replenished_after_connections_close(tmp_path: Path) -> None:
    with bridge(tmp_path) as server:
        pool = server.server._pipe
        assert pool.pool_size == 4
        for _ in range(8):
            client = PipeClient(server.pipe_name)
            client.connect()
            assert client.request("runtime.list", {})["ok"] is True
            client.close()
        assert wait_until(lambda: pool.live_connections == 0)
        with PipeClient(server.pipe_name) as client:
            assert client.request("runtime.list", {})["ok"] is True


def test_client_open_retries_until_a_listener_exists(tmp_path: Path) -> None:
    name = new_pipe_name()
    client = PipeClient(name, deadline_seconds=20.0)
    connected = threading.Event()

    def open_in_background() -> None:
        client.connect()
        connected.set()

    thread = threading.Thread(target=open_in_background, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert not connected.is_set(), "the client connected before any instance existed"
    try:
        with RunningBridge(make_service(tmp_path), pipe_name=name):
            thread.join(timeout=20.0)
            assert connected.is_set(), "the bounded retry never reached a listening instance"
            assert client.request("runtime.list", {})["ok"] is True
    finally:
        client.close()


def test_exact_max_request_bytes_is_dispatched(tmp_path: Path) -> None:
    """§17: the transport limit is inclusive, so a frame of exactly 1 MiB reaches the RPC core.

    No request can be *successful* at that size — the prompt policy is smaller than the
    transport by design — so the boundary proof is that the core answered the request_id with
    its own coded error instead of ``REQUEST_TOO_LARGE``.
    """
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        frame = padded_request_frame("exact", MAX_REQUEST_BYTES)
        assert len(frame) == MAX_REQUEST_BYTES
        client.write(frame)
        response = client.read_frame()
    assert response["request_id"] == "exact"
    assert response["ok"] is False
    assert response["error"]["code"] == "AGENT_PROMPT_TOO_LARGE"


def test_request_one_byte_over_the_limit_is_refused(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        client.write(padded_request_frame("over", MAX_REQUEST_BYTES + 1))
        response = client.read_frame()
    assert response["ok"] is False
    assert response["error"]["code"] == "REQUEST_TOO_LARGE"
    assert response["request_id"] is None


def test_malformed_json_is_invalid_request(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        client.write(b"{not json\n")
        response = client.read_frame()
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_REQUEST"


def test_split_frame_is_reassembled(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        frame = request_frame("split", "runtime.list", {})
        for index in range(0, len(frame), 7):
            client.write(frame[index : index + 7])
            time.sleep(0.005)
        response = client.read_frame()
    assert response["request_id"] == "split"
    assert response["ok"] is True


def test_coalesced_frames_are_served_in_order(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        client.write(
            request_frame("first", "runtime.list", {}) + request_frame("second", "runtime.list", {})
        )
        first = client.read_frame()
        second = client.read_frame()
    assert (first["request_id"], second["request_id"]) == ("first", "second")
    assert first["ok"] is True and second["ok"] is True


def test_partial_frame_does_not_pin_an_instance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("serverfs_agent_bridge.protocol.PIPE_IDLE_TIMEOUT_SECONDS", 0.25)
    with bridge(tmp_path) as server:
        pool = server.server._pipe
        client = PipeClient(server.pipe_name)
        client.connect()
        try:
            client.write(b'{"protocol_version":1,"request_id":"stuck","method')
            assert wait_until(lambda: pool.live_connections > 0, timeout_seconds=5.0), (
                "the partial frame never reached a handler"
            )
            started = time.monotonic()
            assert wait_until(lambda: pool.live_connections == 0, timeout_seconds=10.0), (
                "the idle read never released the instance"
            )
            assert time.monotonic() - started < 10.0
        finally:
            client.close()
        with PipeClient(server.pipe_name) as survivor:
            assert survivor.request("runtime.list", {})["ok"] is True


def test_client_close_mid_frame_frees_the_instance(tmp_path: Path) -> None:
    with bridge(tmp_path) as server:
        client = PipeClient(server.pipe_name)
        client.connect()
        client.write(request_frame("half", "runtime.list", {})[:-3])
        client.close()
        assert wait_until(lambda: not server.server._pipe.live_connections)
        with PipeClient(server.pipe_name) as survivor:
            assert survivor.request("runtime.list", {})["ok"] is True


def test_server_restart_reclaims_the_same_name(tmp_path: Path) -> None:
    server = bridge(tmp_path).start()
    client = PipeClient(server.pipe_name)
    client.connect()
    assert client.request("runtime.list", {})["ok"] is True
    server.stop()
    close_handle(client.handle)
    client.handle = 0
    with RunningBridge(make_service(tmp_path), pipe_name=server.pipe_name) as reopened:
        with PipeClient(reopened.pipe_name) as fresh:
            assert fresh.request("runtime.list", {})["ok"] is True


def test_first_instance_collision_fails_closed(tmp_path: Path) -> None:
    name = new_pipe_name()
    squatting, error = ServerInstance.create(
        name, sddl=pipe_dacl_sddl(current_user_sid()), buffer_size=8192, first_instance=True
    )
    assert squatting is not None, f"could not claim the test name: {error}"
    try:
        server = RunningBridge(make_service(tmp_path), pipe_name=name)
        with pytest.raises(BaseException) as raised:
            server.start()
        assert getattr(raised.value, "code", None) == "PIPE_NAME_UNAVAILABLE"
    finally:
        squatting.close()


def test_explicit_dacl_admits_the_expected_identity() -> None:
    name = new_pipe_name()
    instance, error = ServerInstance.create(
        name, sddl=pipe_dacl_sddl(current_user_sid()), buffer_size=8192, first_instance=True
    )
    assert instance is not None, f"could not create the instance: {error}"
    try:
        connected, code = instance.begin_connect()
        if not connected:
            assert code == ERROR_IO_PENDING, f"ConnectNamedPipe failed with {code}"
        handle, open_error = open_client(name)
        assert handle, f"the expected SID was refused by its own DACL: {open_error}"
        if not connected:
            assert instance.wait(3000)
            connected, _ = instance.complete_connect()
        assert connected
        close_handle(handle)
    finally:
        instance.close()


def test_explicit_dacl_denies_a_non_trustee() -> None:
    """§8: the pipe DACL is the gate, and it denies a SID outside its own allow list.

    Creating a second account is out of scope for Phase B, so the negative case is measured
    from this process's own identity against a DACL that deliberately does not name it — which
    is exactly the ACE a foreign caller would fail on. The default descriptor, measured in
    Phase 0B to grant read to Everyone and Anonymous, is what this replaces.
    """
    name = new_pipe_name()
    instance, error = ServerInstance.create(
        name, sddl=pipe_dacl_sddl(FOREIGN_SID), buffer_size=8192, first_instance=True
    )
    assert instance is not None, f"could not create the instance: {error}"
    try:
        handle, open_error = open_client(name)
        assert handle == 0, "a non-trustee was admitted by the explicit DACL"
        assert open_error == 5, f"expected ERROR_ACCESS_DENIED, got {open_error}"
    finally:
        instance.close()


def test_unauthorized_peer_sid_is_refused_before_dispatch(tmp_path: Path) -> None:
    with bridge(tmp_path) as server:
        # The DACL admitted this client, so the connection is open; what refuses it is the
        # assertion against the measured SID, which runs after the first frame is read and
        # before dispatch. Swapping the expected SID after start keeps those two layers
        # separate, which is exactly the ordering §9 requires.
        server.server.allowed_peer_sid = FOREIGN_SID
        with PipeClient(server.pipe_name) as client:
            response = client.request("runtime.list", {})
        assert response["ok"] is False
        assert response["error"]["code"] == "PEER_NOT_AUTHORIZED"
        # A refused peer is answered before the frame is parsed for dispatch, so the response
        # carries no request_id: nothing reached the RPC core.
        assert response["request_id"] is None


def test_client_measures_server_pid_and_sid(tmp_path: Path) -> None:
    with bridge(tmp_path) as server, PipeClient(server.pipe_name) as client:
        assert client.request("runtime.list", {})["ok"] is True
        assert client.server_pid() == os.getpid()
        assert client.server_sid() == current_user_sid()


def test_idle_timeout_default_is_the_frozen_bound() -> None:
    assert PIPE_IDLE_TIMEOUT_SECONDS == 30.0
