"""Windows Codex transport and Bridge-owned app-server lifecycle.

These are the positive contract of the Windows runtime: the authenticated loopback WebSocket
transport, and the process plus capability-token lifecycle around a Bridge-owned ``codex
app-server``. None of it needs a real Codex CLI or a real provider -- the child is a small script
that speaks the same official protocol shape, which is what keeps the suite deterministic and fast.

Two boundaries are worth stating because they are what the tests exist to hold:

* The loopback listener is the only transport, and it is authenticated. A client with no bearer, or
  the wrong one, is refused. The listener address is literal ``127.0.0.1`` -- ``localhost`` is not a
  security boundary, so the endpoint type refuses anything else.
* The app-server is Bridge-owned on this platform, so its lifecycle is the Bridge's: single-flight
  startup, a fresh high-entropy token per child, a private token file that is destroyed on every
  exit path including failures, and an unexpected child death that a later call recovers from.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from platform_contract import require_windows_kernel
from serverfs_agent_bridge.adapters.codex_transport import (
    LISTENER_UNAVAILABLE_MESSAGE,
    CodexConnection,
    LoopbackWebSocketEndpoint,
    UnixSocketEndpoint,
)
from serverfs_agent_bridge.config import CodexSettings
from serverfs_agent_bridge.errors import BridgeError

require_windows_kernel("the Windows Codex runtime is an authenticated loopback WebSocket listener")

from serverfs_agent_bridge.adapters.codex_windows import (  # noqa: E402 - platform-gated import
    WindowsCodexAppServer,
)

TOKEN = "windows-transport-test-token-value"


@pytest.fixture
def private_state_dir(tmp_path: Path) -> Path:
    """A Bridge-private state root, created the way production creates one.

    ``tmp_path`` inherits pytest's directory ACL, which the private-state contract refuses on
    purpose. Using it unmodified would test a refusal instead of the lifecycle, so the fixture
    establishes the real thing first and the runtime then verifies objects it did not create.
    """
    from serverfs_agent_bridge import private_state

    state = tmp_path / "state"
    private_state.ensure_private_directory(
        state,
        mode=0o700,
        parents=True,
        messages=private_state.DirectoryMessages(
            not_a_directory="state must be a real directory",
            not_owned="state must be owned by the bridge user and mode 0700",
        ),
    )
    return state


def _fake_codex_runner(state_dir: Path, *, announce: bool = True, exit_code: int = 1) -> Path:
    """Write a stand-in ``codex`` that announces an endpoint, then serves a real loopback listener.

    It is a script rather than a mock object so the lifecycle drives a genuine child process: real
    argv handling, real stderr, a real port and a real teardown. What it does not do is model the
    provider, which is what keeps the suite free of network and inference.
    """
    script = state_dir / "fake_codex.py"
    script.write_text(
        """
import asyncio, json, sys
from websockets.asyncio.server import serve

args = sys.argv[1:]
token_file = args[args.index("--ws-token-file") + 1]
open(token_file).read().strip()


async def handler(ws):
    first = json.loads(await ws.recv())
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": first["id"],
                "result": {"userAgent": "codex-app-server/0.159.2", "codexHome": "fake"},
            }
        )
    )
    json.loads(await ws.recv())


async def main():
    server = await serve(handler, host="127.0.0.1", port=0, compression=None)
    port = next(iter(server.sockets)).getsockname()[1]
    if ANNOUNCE:
        print("codex app-server (WebSockets)", file=sys.stderr)
        print("  listening on: ws://127.0.0.1:%d" % port, file=sys.stderr)
        sys.stderr.flush()
        await asyncio.Future()  # serve until the parent stops us
    raise SystemExit(EXIT_CODE)


asyncio.run(main())
""".replace("ANNOUNCE", repr(announce)).replace("EXIT_CODE", str(exit_code)),
        encoding="utf-8",
    )
    return script


def _runtime_with_fake_codex(
    state_dir: Path,
    codex_home: Path,
    *,
    announce: bool = True,
    startup_timeout: float | None = None,
    codex_bin: str | None = None,
) -> WindowsCodexAppServer:
    """A real ``WindowsCodexAppServer`` whose ``codex_bin`` is the stand-in interpreter.

    ``codex_bin`` becomes the *interpreter* and the script is injected through ``argv``, because a
    ``.cmd`` is not a CreateProcess-able image (Phase D measured WinError 193) and ``python.exe``
    genuinely is one. Everything the lifecycle actually does -- argv construction, the token file,
    readiness from the announced endpoint, teardown -- runs unmodified.
    """
    script = _fake_codex_runner(state_dir, announce=announce)
    runtime = WindowsCodexAppServer(
        _settings(codex_home, codex_bin=codex_bin or sys.executable),
        state_dir=state_dir,
        **({"startup_timeout": startup_timeout} if startup_timeout is not None else {}),
    )
    original_build = runtime._build_argv
    runtime._build_argv = lambda: [sys.executable, str(script), *original_build()[1:]]  # type: ignore[method-assign]
    return runtime


async def _serve(require_token: str | None, *, seen: dict[str, Any] | None = None):
    """A loopback JSON-RPC double that answers ``initialize`` and records what it was sent."""
    from websockets.asyncio.server import serve

    async def handler(ws) -> None:
        if require_token is not None:
            if ws.request.headers.get("Authorization") != f"Bearer {require_token}":
                await ws.close(code=1008, reason="unauthorized")
                return
        if seen is not None:
            seen["authorization"] = ws.request.headers.get("Authorization")
            seen["path"] = ws.request.path
        first = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": first["id"],
                    "result": {
                        "userAgent": "codex-app-server/0.159.2",
                        "codexHome": str(Path.home()),
                    },
                }
            )
        )
        second = json.loads(await ws.recv())
        assert second["method"] == "initialized"
        third = json.loads(await ws.recv())
        if seen is not None:
            seen["request"] = third
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": third["id"],
                    "result": {"thread": {"id": "thread-1"}},
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "text": "hi"}},
                }
            )
        )
        # A server->client request. The transport must route it as an event and let the caller
        # answer on this same connection, so the handler waits for that answer rather than assuming
        # one -- otherwise "the client got its event" and "the client never answered" look alike.
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "native-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "pytest -q"},
                }
            )
        )
        request = json.loads(await ws.recv())
        if seen is not None:
            seen["response"] = request
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread-1", "requestId": request["id"]},
                }
            )
        )
        await ws.wait_closed()

    server = await serve(handler, host="127.0.0.1", port=0, compression=None)
    port = next(iter(server.sockets)).getsockname()[1]
    return server, port


def _settings(codex_home: Path, **overrides: Any) -> CodexSettings:
    return CodexSettings(
        enabled=True, codex_home=codex_home, request_timeout_seconds=2, **overrides
    )


# --------------------------------------------------------------------------- endpoint type


def test_loopback_endpoint_refuses_anything_but_literal_loopback() -> None:
    """The listener address is a security boundary, so the type refuses the alternatives."""
    LoopbackWebSocketEndpoint(url="ws://127.0.0.1:9000/rpc", token="t")

    for url in (
        "ws://localhost:9000/rpc",  # resolves through hosts file and DNS; not a boundary
        "ws://0.0.0.0:9000/rpc",
        "ws://192.168.1.10:9000/rpc",
        "wss://127.0.0.1:9000/rpc",  # not the scheme the CLI speaks
        "ws://127.0.0.1/rpc",  # no port
    ):
        with pytest.raises(BridgeError) as caught:
            LoopbackWebSocketEndpoint(url=url, token="t")
        assert (
            "127.0.0.1" in str(caught.value)
            or "ws://" in str(caught.value)
            or "port" in str(caught.value)
        )


def test_loopback_endpoint_never_reveals_the_token() -> None:
    """A credential must not reach a log line through an incidental repr."""
    endpoint = LoopbackWebSocketEndpoint(url="ws://127.0.0.1:9000/rpc", token="super-secret-value")
    assert "super-secret-value" not in repr(endpoint)
    assert "super-secret-value" not in str(endpoint)
    assert "super-secret-value" not in f"{endpoint!r}"
    assert endpoint.authorization == ("Authorization", "Bearer super-secret-value")


# --------------------------------------------------------------------------- transport


@pytest.mark.asyncio
async def test_authenticated_loopback_connection_routes_rpc_and_events() -> None:
    seen: dict[str, Any] = {}
    server, port = await _serve(TOKEN, seen=seen)
    connection = CodexConnection(
        endpoint=LoopbackWebSocketEndpoint(url=f"ws://127.0.0.1:{port}/rpc", token=TOKEN),
        client_name="test-client",
        client_version="test",
        request_timeout=2,
    )
    try:
        await connection.connect()
        assert connection.server_version == "0.159.2"
        result = await connection.request("thread/read", {"threadId": "thread-1"})
        assert result["thread"]["id"] == "thread-1"
        event = await connection.next_event(timeout=2)
        assert event["method"] == "item/completed"
        pending = await connection.next_event(timeout=2)
        assert pending["method"] == "item/commandExecution/requestApproval"
        await connection.respond(pending["id"], {"decision": "decline"})
        # The double records the answer from its own read, so wait for that rather than assuming
        # the respond() call has already been observed on the other side of the socket.
        for _ in range(100):
            if "response" in seen:
                break
            await asyncio.sleep(0.01)
        assert seen["response"]["result"] == {"decision": "decline"}
        assert seen["authorization"] == f"Bearer {TOKEN}"
        assert seen["path"] == "/rpc"
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", [None, "wrong-token"])
async def test_missing_or_wrong_bearer_is_refused(bad_token: str | None) -> None:
    """Phase 0C measured 401 for an anonymous client; the same must hold for a wrong bearer."""
    server, port = await _serve(TOKEN)
    connection = CodexConnection(
        endpoint=LoopbackWebSocketEndpoint(url=f"ws://127.0.0.1:{port}/rpc", token=bad_token or ""),
        client_name="test-client",
        client_version="test",
        request_timeout=2,
    )
    try:
        with pytest.raises(BridgeError) as caught:
            await connection.connect()
        # The refusal message must not echo the credential that was sent.
        assert TOKEN not in str(caught.value)
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_connection_loss_fails_pending_and_reports_transport_closed() -> None:
    """A dropped provider must not leave a caller waiting on a future nobody will complete."""
    from websockets.asyncio.server import serve

    async def handler(ws) -> None:
        first = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": first["id"],
                    "result": {"userAgent": "codex-app-server/0.159.2"},
                }
            )
        )
        json.loads(await ws.recv())  # initialized
        await ws.close()

    server = await serve(handler, host="127.0.0.1", port=0, compression=None)
    port = next(iter(server.sockets)).getsockname()[1]
    connection = CodexConnection(
        endpoint=LoopbackWebSocketEndpoint(url=f"ws://127.0.0.1:{port}/rpc", token=TOKEN),
        client_name="test-client",
        client_version="test",
        request_timeout=2,
    )
    try:
        await connection.connect()
        with pytest.raises(BridgeError):
            await connection.request("thread/read", {"threadId": "x"})
        closed = await connection.next_event(timeout=2)
        assert closed["method"] == "_serverfs/transportClosed"
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_control_connection_never_uses_a_proxy() -> None:
    """The control channel is loopback; the client must be handed ``proxy=None`` explicitly.

    Asserted on the call the transport makes rather than inferred from a task succeeding, because
    an ambient proxy that happened to be absent would make the inference vacuous.
    """
    import serverfs_agent_bridge.adapters.codex_transport as transport_module

    captured: dict[str, Any] = {}

    async def fake_connect(url: str, **kwargs: Any):
        captured["url"] = url
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    original = transport_module.connect
    transport_module.connect = fake_connect
    connection = CodexConnection(
        endpoint=LoopbackWebSocketEndpoint(url="ws://127.0.0.1:9/rpc", token=TOKEN),
        client_name="test-client",
        client_version="test",
    )
    try:
        with pytest.raises(BridgeError) as caught:
            await connection.connect()
        assert captured["proxy"] is None
        assert captured["compression"] is None
        assert captured["additional_headers"] == [("Authorization", f"Bearer {TOKEN}")]
        assert captured["url"] == "ws://127.0.0.1:9/rpc"
        # The operator-facing failure names the platform, not the endpoint or the header.
        assert str(caught.value) == LISTENER_UNAVAILABLE_MESSAGE
        assert TOKEN not in str(caught.value)
        assert "127.0.0.1" not in str(caught.value)
    finally:
        transport_module.connect = original


@pytest.mark.asyncio
async def test_max_message_bound_is_applied_to_the_loopback_socket() -> None:
    server, port = await _serve(TOKEN)
    connection = CodexConnection(
        endpoint=LoopbackWebSocketEndpoint(url=f"ws://127.0.0.1:{port}/rpc", token=TOKEN),
        client_name="test-client",
        client_version="test",
        max_message_bytes=1024,
    )
    try:
        await connection.connect()
        assert connection._open_socket is not None
        # The bound is what the socket was opened with; re-derive it rather than trusting a default.
        import serverfs_agent_bridge.adapters.codex_transport as transport_module

        captured: dict[str, Any] = {}

        async def capture(*args: Any, **kwargs: Any):
            captured.update(kwargs)
            raise RuntimeError("stop")

        original = transport_module.connect
        transport_module.connect = capture
        try:
            with pytest.raises(RuntimeError):
                await connection._open_socket()
        finally:
            transport_module.connect = original
        assert captured["max_size"] == 1024
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- lifecycle


# --------------------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_startup_is_single_flight_and_idempotent(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """Concurrent first calls must converge on one child, not one child per caller.

    Without the lock, runtime.list / model.list / task.submit / reconciliation arriving together
    before any child exists would each spawn an app-server, and the operator would see provider
    processes racing to bind ports.
    """
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    try:
        endpoints = await asyncio.gather(*(runtime.ensure_started() for _ in range(5)))
        assert len({endpoint.url for endpoint in endpoints}) == 1
        assert runtime.process_id is not None
        assert runtime.token_file.exists()
        # Idempotent: a further call reuses the same child rather than starting another.
        again = await runtime.ensure_started()
        assert again.url == endpoints[0].url
    finally:
        await runtime.close()
    assert not runtime.token_file.exists()


@pytest.mark.asyncio
async def test_token_is_fresh_per_child_start(tmp_path: Path, private_state_dir: Path) -> None:
    """A capability token is scoped to one child's lifetime, never reused across restarts."""
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    observed: list[str] = []
    try:
        first = await runtime.ensure_started()
        observed.append(first.token)
        await runtime._teardown_locked()
        second = await runtime.ensure_started()
        observed.append(second.token)
    finally:
        await runtime.close()
    assert observed[0] and observed[1]
    assert observed[0] != observed[1], "a capability token must not survive a child restart"
    # token_urlsafe(32) carries 256 bits, which is the entropy floor this relies on.
    assert len(observed[0]) >= 43


@pytest.mark.asyncio
async def test_token_never_reaches_argv_and_the_file_is_private(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """The token reaches the child through a private file, never through argv or a log line."""
    captured_argv: list[str] = []
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    original_build = runtime._build_argv

    def build_argv() -> list[str]:
        argv = original_build()
        captured_argv.extend(argv)
        return argv

    runtime._build_argv = build_argv  # type: ignore[method-assign]
    try:
        endpoint = await runtime.ensure_started()
        token = endpoint.token
        assert str(runtime.token_file) in captured_argv
        assert token not in " ".join(captured_argv)
        assert runtime.token_file.read_text(encoding="utf-8") == token
        assert token not in repr(endpoint)
    finally:
        await runtime.close()
    assert not runtime.token_file.exists(), "the token file must not outlive the child"


@pytest.mark.asyncio
async def test_token_file_is_rejected_when_its_target_is_unsafe(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """A pre-planted directory at the token path must fail closed before anything is written."""
    from serverfs_agent_bridge import private_state

    token_dir = private_state_dir / "codex"
    private_state.ensure_private_directory(
        token_dir,
        mode=0o700,
        parents=True,
        messages=private_state.DirectoryMessages(
            not_a_directory="token dir must be a real directory",
            not_owned="token dir must be owned by the bridge user and mode 0700",
        ),
    )
    planted = token_dir / "app-server-token"
    planted.mkdir()  # a directory where a secret file must be
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    try:
        # A directory where the secret file belongs is refused. The refusal may surface as the
        # private-state error or as the OS refusing the write; either way nothing is written
        # through the planted object and no child is started.
        with pytest.raises((BridgeError, OSError)):
            await runtime.ensure_started()
        assert planted.is_dir()
        assert runtime.process_id is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_failure_still_removes_the_token_file(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """A refused startup must not leave a credential behind next to no child."""
    runtime = WindowsCodexAppServer(
        _settings(tmp_path / "codex-home", codex_bin=str(tmp_path / "does-not-exist-codex")),
        state_dir=private_state_dir,
    )
    with pytest.raises(BridgeError):
        await runtime.ensure_started()
    assert not runtime.token_file.exists()


@pytest.mark.asyncio
async def test_child_exit_before_announcing_fails_without_waiting_for_the_budget(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """A child that dies during startup must fail at once, not after the whole readiness budget."""
    runtime = _runtime_with_fake_codex(
        private_state_dir, tmp_path / "codex-home", announce=False, startup_timeout=30
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(BridgeError):
        await runtime.ensure_started()
    elapsed = loop.time() - started
    assert elapsed < 20, f"waited {elapsed:.1f}s; an early child exit must fail immediately"
    assert not runtime.token_file.exists()


@pytest.mark.asyncio
async def test_unexpected_child_death_is_recovered_by_the_next_call(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """A provider that dies on its own must not make the runtime permanently unavailable.

    Existing connections see a provider disconnect; the next probe, model list, task or
    reconciliation gets a fresh app-server. No turn is silently retried.
    """
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    try:
        first = await runtime.ensure_started()
        assert runtime._process is not None
        first_pid = runtime._process.pid
        runtime._process.kill()
        await runtime._process.wait()
        second = await runtime.ensure_started()
        assert runtime.process_id is not None and runtime.process_id != first_pid
        assert second.url != first.url
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_a_stale_private_token_file_is_overwritten_not_refused(
    tmp_path: Path, private_state_dir: Path
) -> None:
    """A file left by a crashed Bridge must not block startup or leak the old secret.

    The object is verified private first, so overwriting it with a fresh secret is safe. Refusing
    would make a crash permanently disable the runtime; blind deletion would be an attack on an
    attacker-chosen path.
    """
    from serverfs_agent_bridge import private_state

    token_dir = private_state_dir / "codex"
    private_state.ensure_private_directory(
        token_dir,
        mode=0o700,
        parents=True,
        messages=private_state.DirectoryMessages(
            not_a_directory="token dir must be a real directory",
            not_owned="token dir must be owned by the bridge user and mode 0700",
        ),
    )
    stale = token_dir / "app-server-token"
    private_state.ensure_private_file(
        stale, mode=0o600, not_regular="token path must be a regular file"
    )
    stale.write_text("stale-token-from-a-previous-crash", encoding="utf-8")
    runtime = _runtime_with_fake_codex(private_state_dir, tmp_path / "codex-home")
    try:
        endpoint = await runtime.ensure_started()
        assert stale.read_text(encoding="utf-8") == endpoint.token
        assert "stale-token-from-a-previous-crash" not in stale.read_text(encoding="utf-8")
    finally:
        await runtime.close()


def test_unix_endpoint_still_addresses_the_managed_daemon_socket(tmp_path: Path) -> None:
    """The Linux endpoint shape is unchanged: it is the configured control socket, nothing else."""
    endpoint = UnixSocketEndpoint(tmp_path / "app-server-control.sock")
    assert endpoint.kind == "unix"
    assert endpoint.path.name == "app-server-control.sock"
