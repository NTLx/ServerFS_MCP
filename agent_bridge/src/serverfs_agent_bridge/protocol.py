"""Versioned JSON-lines RPC over a local Unix-domain socket."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Any

from .errors import BridgeError
from .service import BridgeService

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
_ALLOWED_TOP_LEVEL = frozenset({"protocol_version", "request_id", "method", "params"})


class BridgeProtocolServer:
    def __init__(
        self,
        *,
        service: BridgeService,
        socket_path: Path,
        allowed_peer_uid: int | None = None,
        allowed_peer_gid: int | None = None,
    ):
        self.service = service
        self.socket_path = socket_path
        if not self.socket_path.is_absolute():
            raise ValueError("socket_path must be absolute")
        self._shared_gid_explicit = allowed_peer_gid is not None
        self.allowed_peer_uid = (
            os.getuid()
            if allowed_peer_uid is None and allowed_peer_gid is None
            else allowed_peer_uid
        )
        self.allowed_peer_gid = (
            os.getgid()
            if allowed_peer_uid is None and allowed_peer_gid is None
            else allowed_peer_gid
        )
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        self._prepare_socket_parent()
        await self._prepare_socket_path()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=MAX_REQUEST_BYTES + 1,
        )
        socket_stat = self.socket_path.stat()
        self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
        socket_mode = 0o660 if self._shared_gid_explicit else 0o600
        if self._shared_gid_explicit:
            try:
                os.chown(self.socket_path, -1, self.allowed_peer_gid)
            except OSError as exc:
                await self.close()
                raise BridgeError(
                    "SOCKET_DIRECTORY_UNSAFE",
                    "socket group cannot be set to the authorized peer gid",
                ) from exc
        os.chmod(self.socket_path, socket_mode)

    async def _prepare_socket_path(self) -> None:
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISSOCK(existing.st_mode):
            raise BridgeError("SOCKET_PATH_IN_USE", "configured socket path already exists")
        if existing.st_uid != os.getuid():
            raise BridgeError("SOCKET_PATH_IN_USE", "configured socket path has another owner")

        identity = (existing.st_dev, existing.st_ino)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path=str(self.socket_path)),
                timeout=0.5,
            )
        except FileNotFoundError:
            return
        except TimeoutError as exc:
            raise BridgeError(
                "SOCKET_PATH_IN_USE",
                "configured socket path did not become connectable or stale in time",
            ) from exc
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise BridgeError(
                    "SOCKET_PATH_IN_USE",
                    "configured socket path cannot be safely recovered",
                ) from exc
        else:
            writer.close()
            await writer.wait_closed()
            raise BridgeError("SOCKET_PATH_IN_USE", "configured socket path is active")

        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.getuid()
            or (current.st_dev, current.st_ino) != identity
        ):
            raise BridgeError(
                "SOCKET_PATH_IN_USE",
                "configured socket path changed during stale-socket recovery",
            )
        self.socket_path.unlink()

    def _prepare_socket_parent(self) -> None:
        parent = self.socket_path.parent
        missing: list[Path] = []
        current = parent
        while True:
            try:
                current_stat = current.lstat()
            except FileNotFoundError as exc:
                missing.append(current)
                if current.parent == current:
                    raise BridgeError(
                        "SOCKET_DIRECTORY_UNSAFE", "socket directory has no existing anchor"
                    ) from exc
                current = current.parent
                continue
            if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                raise BridgeError("SOCKET_DIRECTORY_UNSAFE", "socket directory is not a directory")
            break

        created_mode = 0o750 if self._shared_gid_explicit else 0o700
        for directory in reversed(missing):
            os.mkdir(directory, created_mode)
            os.chmod(directory, created_mode)

        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise BridgeError("SOCKET_DIRECTORY_UNSAFE", "socket directory is not a directory")
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
            raise BridgeError(
                "SOCKET_DIRECTORY_UNSAFE",
                "socket directory must be owned by the bridge user and not group/world writable",
            )
        if self._shared_gid_explicit and parent_stat.st_gid != self.allowed_peer_gid:
            try:
                os.chown(parent, -1, self.allowed_peer_gid)
            except OSError as exc:
                raise BridgeError(
                    "SOCKET_DIRECTORY_UNSAFE",
                    "socket directory group cannot be set to the authorized peer gid",
                ) from exc
        os.chmod(parent, created_mode)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists() or self.socket_path.is_symlink():
            try:
                socket_stat = self.socket_path.lstat()
            except FileNotFoundError:
                self._socket_identity = None
                return
            if (
                self._socket_identity is not None
                and stat.S_ISSOCK(socket_stat.st_mode)
                and (socket_stat.st_dev, socket_stat.st_ino) == self._socket_identity
            ):
                self.socket_path.unlink()
        self._socket_identity = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            self._check_peer(writer)
            while True:
                try:
                    raw = await reader.readline()
                except asyncio.LimitOverrunError:
                    await _write_response(
                        writer,
                        _error_response(
                            None,
                            "REQUEST_TOO_LARGE",
                            "bridge request exceeds configured limit",
                        ),
                    )
                    break
                if not raw:
                    break
                if len(raw) > MAX_REQUEST_BYTES:
                    await _write_response(
                        writer,
                        _error_response(
                            None,
                            "REQUEST_TOO_LARGE",
                            "bridge request exceeds configured limit",
                        ),
                    )
                    break
                response = await self._dispatch_raw(raw)
                await _write_response(writer, response)
        except BridgeError as exc:
            await _write_response(
                writer,
                {"request_id": None, "ok": False, "error": exc.to_dict()},
            )
        except Exception:
            await _write_response(
                writer,
                {
                    "request_id": None,
                    "ok": False,
                    "error": {"code": "BRIDGE_PROTOCOL_ERROR", "message": "protocol failure"},
                },
            )
        finally:
            writer.close()
            await writer.wait_closed()

    def _check_peer(self, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable")
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError as exc:
            raise BridgeError("PEER_NOT_AUTHORIZED", "peer credentials are unavailable") from exc
        _, uid, gid = struct.unpack("3i", raw)
        if self.allowed_peer_uid is not None and uid != self.allowed_peer_uid:
            raise BridgeError("PEER_NOT_AUTHORIZED", "peer uid is not authorized")
        if self.allowed_peer_gid is not None and gid != self.allowed_peer_gid:
            raise BridgeError("PEER_NOT_AUTHORIZED", "peer gid is not authorized")

    async def _dispatch_raw(self, raw: bytes) -> dict[str, Any]:
        try:
            request = json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _error_response(None, "INVALID_REQUEST", "request must be valid UTF-8 JSON")
        request_id = (
            request.get("request_id")
            if isinstance(request, dict)
            and isinstance(request.get("request_id"), str)
            and request.get("request_id")
            else None
        )
        try:
            if not isinstance(request, dict) or set(request) != _ALLOWED_TOP_LEVEL:
                raise BridgeError("INVALID_REQUEST", "request fields do not match protocol")
            if type(request["protocol_version"]) is not int:
                raise BridgeError("INVALID_REQUEST", "protocol_version must be an integer")
            if request["protocol_version"] != PROTOCOL_VERSION:
                raise BridgeError(
                    "PROTOCOL_VERSION_UNSUPPORTED", "unsupported bridge protocol version"
                )
            if not isinstance(request_id, str) or not request_id:
                raise BridgeError("INVALID_REQUEST", "request_id must be a non-empty string")
            method = request["method"]
            params = request["params"]
            if not isinstance(method, str) or not method or not isinstance(params, dict):
                raise BridgeError("INVALID_REQUEST", "method and params have invalid types")
            result = await self._dispatch(method, params)
            return {"request_id": request_id, "ok": True, "result": result}
        except BridgeError as exc:
            return {"request_id": request_id, "ok": False, "error": exc.to_dict()}
        except Exception:
            return _error_response(request_id, "BRIDGE_PROTOCOL_ERROR", "protocol failure")

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "runtime.list":
            _require_keys(params, set())
            return await self.service.list_runtimes()
        if method == "task.submit":
            _require_keys(
                params,
                {"runtime", "workdir", "path", "profile", "prompt"},
                optional={"continue_from_task_id"},
            )
            _require_types(
                params,
                {"runtime": str, "workdir": str, "path": str, "profile": str, "prompt": str},
                optional={"continue_from_task_id": (str, type(None))},
            )
            return await self.service.submit_task(**params)
        if method == "task.get":
            _require_keys(params, {"task_id"})
            _require_types(params, {"task_id": str})
            return self.service.get_task(params["task_id"])
        if method == "task.events":
            _require_keys(params, {"task_id"}, optional={"after_event_id", "limit"})
            _require_types(
                params,
                {"task_id": str},
                optional={"after_event_id": int, "limit": int},
            )
            return self.service.read_events(**params)
        if method == "task.approval.respond":
            _require_keys(
                params,
                {"task_id", "request_id", "decision"},
                optional={"granted_permission_ids"},
            )
            _require_types(
                params,
                {"task_id": str, "request_id": str, "decision": str},
                optional={"granted_permission_ids": list},
            )
            if "granted_permission_ids" in params and any(
                not isinstance(item, str) for item in params["granted_permission_ids"]
            ):
                raise BridgeError("INVALID_REQUEST", "permission ids must be strings")
            return await self.service.respond_approval(**params)
        if method == "task.question.answer":
            _require_keys(params, {"task_id", "request_id", "answers"})
            _require_types(params, {"task_id": str, "request_id": str, "answers": list})
            return await self.service.answer_question(**params)
        if method == "task.message.send":
            _require_keys(params, {"task_id", "message"})
            _require_types(params, {"task_id": str, "message": str})
            return await self.service.send_message(**params)
        if method == "task.cancel":
            _require_keys(params, {"task_id"})
            _require_types(params, {"task_id": str})
            return await self.service.cancel_task(params["task_id"])
        raise BridgeError("METHOD_NOT_FOUND", f"unknown bridge method: {method}")


def _require_keys(
    params: dict[str, Any], required: set[str], *, optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - set(params)
    unknown = set(params) - required - optional
    if missing or unknown:
        raise BridgeError("INVALID_REQUEST", "method parameters do not match protocol")


async def _write_response(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
    data = (
        json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    if len(data) > MAX_RESPONSE_BYTES:
        data = (
            json.dumps(
                _error_response(
                    response.get("request_id"),
                    "RESPONSE_TOO_LARGE",
                    "bridge response exceeds configured limit",
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    writer.write(data)
    await writer.drain()


def _error_response(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    return {"request_id": request_id, "ok": False, "error": {"code": code, "message": message}}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _require_types(
    params: dict[str, Any],
    required: dict[str, type],
    *,
    optional: dict[str, type | tuple[type, ...]] | None = None,
) -> None:
    for key, expected in {**required, **(optional or {})}.items():
        if key not in params:
            continue
        value = params[key]
        if expected is int and isinstance(value, bool):
            raise BridgeError("INVALID_REQUEST", f"parameter {key} has invalid type")
        if not isinstance(value, expected):
            raise BridgeError("INVALID_REQUEST", f"parameter {key} has invalid type")
