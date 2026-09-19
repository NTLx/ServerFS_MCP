"""Remote Codex App Server transport over the managed daemon Unix socket.

Codex's managed app-server control socket carries WebSocket frames over AF_UNIX.
JSON-RPC messages are encoded as JSON text frames.  This module intentionally
contains only transport/routing; provider semantics live in codex.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed

from ..errors import BridgeError

_UDS_HANDSHAKE_URI = "ws://localhost/rpc"
_DEFAULT_MAX_MESSAGE_BYTES = 128 * 1024 * 1024


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
        socket_path: Path,
        client_name: str,
        client_version: str,
        request_timeout: float = 10.0,
        max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        self.socket_path = socket_path
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
            self._ws = await unix_connect(
                path=str(self.socket_path),
                uri=_UDS_HANDSHAKE_URI,
                open_timeout=self.request_timeout,
                close_timeout=5,
                max_size=self.max_message_bytes,
                # The daemon closes the connection without an HTTP response when
                # the client offers permessage-deflate, so compression must stay
                # off.  Verified against a running managed daemon.
                compression=None,
                proxy=None,
            )
        except Exception as exc:
            raise BridgeError(
                "AGENT_RUNTIME_NOT_READY",
                "Codex App Server daemon control socket is unavailable",
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
