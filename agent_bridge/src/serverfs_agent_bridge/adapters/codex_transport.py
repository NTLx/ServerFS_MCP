"""Remote Codex App Server transport.

Two endpoint shapes reach the same RPC engine. On Linux the managed app-server control socket
carries WebSocket frames over AF_UNIX. On Windows a Bridge-owned ``codex app-server`` child listens
on an authenticated loopback WebSocket endpoint. JSON-RPC messages are encoded as JSON text frames
in both cases.  This module intentionally contains only transport/routing; provider semantics live
in codex.py, and process/token lifecycle for the Windows child lives in codex_windows.py.

The RPC engine -- request framing, the reader loop, pending futures, the event queue and server
request routing -- is deliberately a single implementation. Only the socket handshake differs
between platforms, so a second copy of the framing would be a second set of defects.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed

from ..errors import BridgeError

_UDS_HANDSHAKE_URI = "ws://localhost/rpc"
_DEFAULT_MAX_MESSAGE_BYTES = 128 * 1024 * 1024
CONTROL_SOCKET_UNAVAILABLE_MESSAGE = "Codex App Server daemon control socket is unavailable"
LISTENER_UNAVAILABLE_MESSAGE = "Codex App Server loopback listener is unavailable"

#: The Windows listener is addressed by literal loopback. ``localhost`` resolves through hosts
#: file and DNS machinery and is therefore not a security boundary; Phase 0C measured the CLI
#: binding 127.0.0.1 only, so the strict spelling is what the Bridge is willing to dial.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1"})


@dataclass(frozen=True)
class UnixSocketEndpoint:
    """The Linux managed-daemon control socket (AF_UNIX)."""

    path: Path

    kind: str = "unix"


@dataclass(frozen=True)
class LoopbackWebSocketEndpoint:
    """A Bridge-owned Windows app-server listener, authenticated by a capability token.

    The token is a credential. It is excluded from ``repr`` so it cannot reach a log line through
    an incidental interpolation of an endpoint object, and it is never carried in the URL.
    """

    url: str
    token: str = field(repr=False)

    kind: str = "loopback-ws"

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme != "ws":
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                "Codex loopback endpoint must be a ws:// URL",
            )
        if parts.hostname not in _LOOPBACK_HOSTS:
            # A listener reachable off-host is a different security posture than the one Phase 0C
            # measured and the one this runtime is specified against. Refuse rather than warn.
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                "Codex loopback endpoint must address literal 127.0.0.1",
            )
        if parts.port is None:
            raise BridgeError(
                "AGENT_PROVIDER_ERROR",
                "Codex loopback endpoint must name an explicit port",
            )

    @property
    def authorization(self) -> tuple[str, str]:
        """The header tuple for this endpoint. Callers pass it straight to the WS client.

        Returning it from one place keeps the header name and scheme in a single definition, so a
        test can assert the client is given exactly this and nothing ambient.
        """
        return ("Authorization", f"Bearer {self.token}")

    def __repr__(self) -> str:
        return f"LoopbackWebSocketEndpoint(url={self.url!r}, token=<redacted>)"


CodexEndpoint = UnixSocketEndpoint | LoopbackWebSocketEndpoint


class CodexRpcError(BridgeError):
    """JSON-RPC error returned by Codex App Server."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None):
        super().__init__("AGENT_PROVIDER_ERROR", message)
        self.rpc_code = code
        self.data = data


class CodexConnection:
    """One initialized Codex App Server client connection."""

    def __init__(
        self,
        *,
        endpoint: CodexEndpoint,
        client_name: str,
        client_version: str,
        request_timeout: float = 10.0,
        max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        self.endpoint = endpoint
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout = request_timeout
        self.max_message_bytes = max_message_bytes

        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False
        self.server_version: str | None = None
        self.codex_home: str | None = None

    async def connect(self) -> None:
        if self._closed:
            raise BridgeError("AGENT_PROVIDER_DISCONNECTED", "Codex connection is closed")
        if self._ws is not None:
            return
        try:
            self._ws = await self._open_socket()
        except Exception as exc:
            # The message is fixed per platform and carries no endpoint detail: the Windows URL
            # contains a port and the failure could echo a header, so neither reaches the operator.
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                self._unavailable_message(),
            ) from exc

        self._reader = asyncio.create_task(self._reader_loop(), name="serverfs-codex-rpc-reader")
        try:
            initialized = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": "ServerFS Agent Bridge",
                        "version": self.client_version,
                    },
                    "capabilities": {"experimentalApi": True},
                },
                request_id="initialize",
            )
            if not isinstance(initialized, dict):
                raise BridgeError(
                    "AGENT_PROVIDER_ERROR", "Codex initialize returned an invalid result"
                )
            user_agent = initialized.get("userAgent")
            if isinstance(user_agent, str):
                self.server_version = _version_from_user_agent(user_agent)
            codex_home = initialized.get("codexHome")
            if isinstance(codex_home, str):
                self.codex_home = codex_home
            await self.notify("initialized", {})
        except Exception:
            await self.close()
            raise

    async def _open_socket(self) -> ClientConnection:
        """Perform the platform handshake. Everything after this is shared RPC machinery."""
        if isinstance(self.endpoint, LoopbackWebSocketEndpoint):
            return await connect(
                self.endpoint.url,
                additional_headers=[self.endpoint.authorization],
                open_timeout=self.request_timeout,
                close_timeout=5,
                max_size=self.max_message_bytes,
                # Two independent reasons compression stays off, both measured: the daemon closes
                # a UDS connection without an HTTP response when permessage-deflate is offered,
                # and the Windows listener is the same server speaking over TCP.
                compression=None,
                # Hard requirement, not a default: the control channel is loopback and must never
                # be reachable through an ambient system proxy. The child also gets a NO_PROXY
                # bypass, and this is the second layer. Relying on NO_PROXY alone would make the
                # control channel's reachability depend on whatever the host happens to export.
                proxy=None,
            )
        return await unix_connect(
            path=str(self.endpoint.path),
            uri=_UDS_HANDSHAKE_URI,
            open_timeout=self.request_timeout,
            close_timeout=5,
            max_size=self.max_message_bytes,
            compression=None,
            proxy=None,
        )

    def _unavailable_message(self) -> str:
        if isinstance(self.endpoint, LoopbackWebSocketEndpoint):
            return LISTENER_UNAVAILABLE_MESSAGE
        return CONTROL_SOCKET_UNAVAILABLE_MESSAGE

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> Any:
        ws = self._require_connection()
        if request_id is None:
            request_id = str(self._next_id)
            self._next_id += 1
        if request_id in self._pending:
            raise BridgeError("AGENT_PROVIDER_ERROR", "duplicate Codex JSON-RPC request id")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            try:
                return await asyncio.wait_for(future, timeout=self.request_timeout)
            except TimeoutError as exc:
                raise BridgeError(
                    "AGENT_PROVIDER_DISCONNECTED",
                    f"Codex App Server timed out responding to {method}",
                ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send(
            self._require_connection(),
            {"jsonrpc": "2.0", "method": method, "params": params},
        )

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._send(
            self._require_connection(),
            {"jsonrpc": "2.0", "id": request_id, "result": result},
        )

    async def respond_error(
        self,
        request_id: Any,
        *,
        code: int = -32601,
        message: str = "unsupported request",
    ) -> None:
        await self._send(
            self._require_connection(),
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
        )

    async def next_event(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._events.get()
        try:
            return await asyncio.wait_for(self._events.get(), timeout=timeout)
        except TimeoutError as exc:
            raise BridgeError(
                "AGENT_PROVIDER_DISCONNECTED",
                "Codex App Server produced no events before the idle timeout",
            ) from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reader = self._reader
        self._reader = None
        ws = self._ws
        self._ws = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self._fail_pending("Codex App Server connection closed")

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue

                if "id" in message and "method" not in message:
                    request_id = str(message["id"])
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message.get("error")
                        if isinstance(error, dict):
                            future.set_exception(
                                CodexRpcError(
                                    str(error.get("message", "Codex JSON-RPC error")),
                                    code=error.get("code")
                                    if isinstance(error.get("code"), int)
                                    else None,
                                    data=error.get("data"),
                                )
                            )
                        else:
                            future.set_exception(CodexRpcError("Codex JSON-RPC error"))
                    else:
                        future.set_result(message.get("result"))
                    continue

                if isinstance(message.get("method"), str):
                    await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            self._fail_pending("Codex App Server connection was lost")
            await self._events.put(
                {
                    "method": "_serverfs/transportClosed",
                    "params": {},
                }
            )

    async def _send(self, ws: ClientConnection, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise BridgeError("AGENT_PROVIDER_ERROR", "invalid Codex JSON-RPC payload") from exc
        if len(encoded.encode("utf-8")) > self.max_message_bytes:
            raise BridgeError("AGENT_PROVIDER_ERROR", "Codex JSON-RPC payload is too large")
        async with self._send_lock:
            try:
                await ws.send(encoded)
            except Exception as exc:
                raise BridgeError(
                    "AGENT_PROVIDER_DISCONNECTED", "Codex App Server send failed"
                ) from exc

    def _require_connection(self) -> ClientConnection:
        if self._closed or self._ws is None:
            raise BridgeError("AGENT_PROVIDER_DISCONNECTED", "Codex connection is not open")
        return self._ws

    def _fail_pending(self, message: str) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(BridgeError("AGENT_PROVIDER_DISCONNECTED", message))


def _version_from_user_agent(user_agent: str) -> str | None:
    if "/" not in user_agent:
        return None
    tail = user_agent.split("/", 1)[1].strip()
    return tail.split(maxsplit=1)[0] if tail else None
