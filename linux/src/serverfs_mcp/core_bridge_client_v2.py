"""Opt-in signed client for the macOS Core Bridge v2 Unix socket."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_METHODS = frozenset(
    {
        "bridge.ping",
        "runtime.list",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
        "runtime.models",
        "task.submit",
        "task.list",
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)


class CoreBridgeClientV2Error(Exception):
    """Base class for sanitized v2 client errors."""


class CoreBridgeUnavailable(CoreBridgeClientV2Error):
    """The local v2 endpoint could not be reached safely."""


class CoreBridgeRemoteError(CoreBridgeClientV2Error):
    """A normalized error returned by the Bridge."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CoreBridgeClientV2:
    """Caller-configured client; it never provisions or persists credentials."""

    def __init__(
        self,
        socket_path: Path,
        *,
        bridge_id: str,
        core_id: str,
        key_id: str,
        secret: bytes,
        workdir_alias: str,
        policy_revision: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not sys.platform.startswith("linux"):
            raise ValueError("Core Bridge v2 client is available only on Linux")
        if not isinstance(socket_path, Path) or not socket_path.is_absolute():
            raise ValueError("Core Bridge socket path must be absolute")
        if any(
            not isinstance(value, str) or not _ID.fullmatch(value)
            for value in (bridge_id, core_id, key_id)
        ):
            raise ValueError("Core Bridge identity is invalid")
        if not isinstance(secret, bytes) or len(secret) != 32:
            raise ValueError("Core Bridge key must be exactly 32 bytes")
        if not isinstance(workdir_alias, str) or not _ALIAS.fullmatch(workdir_alias):
            raise ValueError("Core Bridge workdir alias is invalid")
        if not isinstance(policy_revision, str) or not _DIGEST.fullmatch(policy_revision):
            raise ValueError("Core Bridge policy revision is invalid")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300:
            raise ValueError("Core Bridge timeout is invalid")
        self._socket_path = socket_path
        self._bridge_id = bridge_id
        self._core_id = core_id
        self._key_id = key_id
        self._secret = secret
        self._workdir_alias = workdir_alias
        self._policy_revision = policy_revision
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return "CoreBridgeClientV2(<credentials hidden>)"

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or method not in _METHODS:
            raise ValueError("Core Bridge method is invalid")
        if not isinstance(params, dict):
            raise ValueError("Core Bridge params are invalid")
        if method == "task.list":
            if "workdir" in params and params["workdir"] != self._workdir_alias:
                raise ValueError("task discovery requires the configured workdir alias")
            params = {key: value for key, value in params.items() if key != "workdir"}
        elif "workdir" in params:
            raise ValueError("Core Bridge params are invalid")
        request_id = f"core_{secrets.token_hex(16)}"
        unsigned = {
            "protocol_version": 2,
            "bridge_id": self._bridge_id,
            "core_id": self._core_id,
            "key_id": self._key_id,
            "request_id": request_id,
            "method": method,
            "params": {**params, "workdir": self._workdir_alias},
            "policy_revision": self._policy_revision,
            "issued_at": int(time.time()),
            "nonce": secrets.token_hex(16),
        }
        try:
            signature = hmac.new(
                self._secret, _canonical_message(unsigned), hashlib.sha256
            ).hexdigest()
            encoded = (
                json.dumps(
                    {**unsigned, "signature": signature},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise CoreBridgeClientV2Error("invalid Core Bridge request") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise CoreBridgeClientV2Error("Core Bridge request exceeds configured limit")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw = await self._exchange(encoded)
            return _parse_response(raw, request_id)
        except CoreBridgeRemoteError:
            raise
        except TimeoutError as exc:
            raise CoreBridgeUnavailable("Core Bridge request timed out") from exc
        except (ConnectionError, OSError) as exc:
            raise CoreBridgeUnavailable("Core Bridge is unavailable") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise CoreBridgeClientV2Error("Core Bridge returned an invalid response") from exc

    async def _exchange(self, encoded: bytes) -> bytes:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(self._socket_path), limit=MAX_RESPONSE_BYTES + 1
            )
            writer.write(encoded)
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("Bridge closed the connection")
            if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("Bridge response exceeds limit or is incomplete")
            return raw[:-1]
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass


def _canonical_message(envelope: dict[str, Any]) -> bytes:
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _parse_response(raw: bytes, request_id: str) -> dict[str, Any]:
    response = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(response, dict) or response.get("request_id") != request_id:
        raise ValueError("response request ID mismatch")
    if type(response.get("ok")) is not bool:
        raise ValueError("invalid response status")
    if response["ok"]:
        if set(response) != {"request_id", "ok", "result"} or not isinstance(
            response["result"], dict
        ):
            raise ValueError("invalid success response")
        return response["result"]
    if set(response) != {"request_id", "ok", "error"} or not isinstance(response["error"], dict):
        raise ValueError("invalid error response")
    error = response["error"]
    if set(error) != {"code", "message"} or not all(
        isinstance(error[k], str) and error[k] for k in error
    ):
        raise ValueError("invalid error fields")
    raise CoreBridgeRemoteError(error["code"], error["message"])


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
