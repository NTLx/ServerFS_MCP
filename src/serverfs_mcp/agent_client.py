"""Thin client for the host-side ServerFS Agent Bridge UDS protocol."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576


class AgentBridgeClientError(Exception):
    """Base class for agent-bridge client failures."""


class AgentBridgeUnavailable(AgentBridgeClientError):
    """The local bridge socket could not be reached safely."""


class AgentBridgeRemoteError(AgentBridgeClientError):
    """A normalized error returned by the bridge."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AgentBridgeClient:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 15.0):
        if not socket_path.is_absolute():
            raise ValueError("agent bridge socket path must be absolute")
        if timeout_seconds <= 0:
            raise ValueError("agent bridge timeout must be positive")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("bridge method must be a non-empty string")
        request_id = f"mcp_{secrets.token_hex(12)}"
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": method,
            "params": params,
        }
        try:
            encoded = (
                json.dumps(
                    request,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise AgentBridgeClientError("invalid bridge request payload") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AgentBridgeClientError("bridge request exceeds configured limit")

        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._exchange(encoded, request_id)
        except AgentBridgeRemoteError:
            raise
        except TimeoutError as exc:
            raise AgentBridgeUnavailable("agent bridge request timed out") from exc
        except (ConnectionError, OSError) as exc:
            raise AgentBridgeUnavailable("agent bridge is unavailable") from exc
        except ValueError as exc:
            raise AgentBridgeClientError("agent bridge returned an invalid response") from exc

    async def _exchange(self, encoded: bytes, request_id: str) -> dict[str, Any]:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(self.socket_path),
                limit=MAX_RESPONSE_BYTES + 1,
            )
            writer.write(encoded)
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("agent bridge closed the connection")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("agent bridge response exceeds configured limit")
            response = json.loads(raw, parse_constant=_reject_json_constant)
            return _parse_response(response, request_id)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass


def _parse_response(response: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("agent bridge returned a non-object response")
    if response.get("request_id") != request_id:
        raise ValueError("agent bridge response request_id mismatch")
    ok = response.get("ok")
    if type(ok) is not bool:
        raise ValueError("agent bridge response has invalid ok field")

    if ok:
        if set(response) != {"request_id", "ok", "result"}:
            raise ValueError("agent bridge success response fields do not match protocol")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("agent bridge result must be an object")
        return result

    if set(response) != {"request_id", "ok", "error"}:
        raise ValueError("agent bridge error response fields do not match protocol")
    error = response.get("error")
    if not isinstance(error, dict):
        raise ValueError("agent bridge error must be an object")
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not code or not isinstance(message, str):
        raise ValueError("agent bridge error fields are invalid")
    raise AgentBridgeRemoteError(code, message)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
