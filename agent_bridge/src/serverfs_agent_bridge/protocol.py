"""Versioned JSON-lines RPC over the platform's local IPC endpoint.

The framing, size bounds, protocol version and error vocabulary below are the frozen
provider-neutral Bridge contract and are shared by both platforms. The endpoint itself is a
§3 seam: an AF_UNIX socket guarded by ``SO_PEERCRED`` on Linux, and a Named Pipe guarded by a
measured client SID on Windows (see ``local_ipc.py`` for that twin and Phase 0B for the
measurements behind it).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

from .errors import BridgeError
from .local_ipc import (
    PIPE_NAMESPACE,
    authorize_darwin_peer,
    authorize_posix_peer,
    authorize_windows_peer,
    derive_pipe_name,
    max_socket_path_bytes,
    measure_darwin_peer,
    measure_posix_peer,
)
from .platform_seams import DARWIN, WINDOWS
from .service import BridgeService

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
_ALLOWED_TOP_LEVEL = frozenset({"protocol_version", "request_id", "method", "params"})

#: Pipe sizing is a platform property, not operator configuration in v0.11: a byte pipe serves
#: at most as many simultaneous clients as there are listening instances, so the Bridge keeps
#: a pool of four (§13), and the idle read bound follows the existing RPC timeout philosophy
#: (§18). Phase D may expose both as configuration if an operator ever needs to change them.
PIPE_POOL_SIZE = 4
PIPE_IDLE_TIMEOUT_SECONDS = 30.0


class BridgeProtocolServer:
    def __init__(
        self,
        *,
        service: BridgeService,
        socket_path: Path | None = None,
        allowed_peer_uid: int | None = None,
        allowed_peer_gid: int | None = None,
        allowed_peer_sid: str | None = None,
        pipe_name: str | None = None,
    ):
        self.service = service
        self.socket_path = socket_path
        self._shared_gid_explicit = allowed_peer_gid is not None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._pipe: Any = None
        self.allowed_peer_uid = allowed_peer_uid
        self.allowed_peer_gid = allowed_peer_gid
        self.allowed_peer_sid = allowed_peer_sid
        self._pipe_name = pipe_name
        if WINDOWS:
            # The endpoint behind this seam is a pipe name, not a path, so a Windows Bridge is
            # configured without one. The name must still be a pipe namespace path, and the
            # production name is derived from the Bridge user's SID — never from a username,
            # host or PID (§6). Tests drive a second Bridge on a throwaway name, so only the
            # namespace is validated here; `derive_pipe_name` owns the §6 shape.
            if self._pipe_name is not None and not self._pipe_name.startswith(PIPE_NAMESPACE):
                raise ValueError(f"pipe_name must start with {PIPE_NAMESPACE}")
            # The Windows Bridge and ServerFS run as one interactive user, so the expected peer
            # SID defaults to this process's own identity: measured, then asserted, never
            # inferred from the pipe name, a username or a PID (§11).
            if self.allowed_peer_sid is None:
                from .windows_security import current_user_sid

                self.allowed_peer_sid = current_user_sid()
            if self._pipe_name is None:
                self._pipe_name = derive_pipe_name(self.allowed_peer_sid)
            return
        if socket_path is None:
            raise ValueError("socket_path must be set where the local endpoint is a socket")
        if not self.socket_path.is_absolute():
            raise ValueError("socket_path must be absolute")
        if allowed_peer_uid is None and allowed_peer_gid is None:
            self.allowed_peer_uid = os.getuid()
            self.allowed_peer_gid = os.getgid()

    async def start(self) -> None:
        if WINDOWS:
            await self._start_pipe()
            return
        if DARWIN and len(os.fsencode(str(self.socket_path))) > max_socket_path_bytes():
            # sun_path is 104 bytes on Darwin including the NUL; refuse a
            # too-long endpoint before bind instead of discovering it as a
            # confusing bind failure (dev_plan_v0.13.md §11 D2).
            raise BridgeError(
                "SOCKET_PATH_TOO_LONG",
                "socket path exceeds the Darwin sun_path limit",
            )
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

    async def _start_pipe(self) -> None:
        from .local_ipc import create_pipe_endpoint
        from .windows_security import pipe_dacl_sddl

        assert self._pipe_name is not None and self.allowed_peer_sid is not None
        self._pipe = create_pipe_endpoint(
            pipe_name=self._pipe_name,
            # The default pipe descriptor grants read to Everyone and Anonymous (Phase 0B),
            # so the DACL is explicit and protected rather than inherited.
            dacl_sddl=pipe_dacl_sddl(self.allowed_peer_sid),
            limit=MAX_REQUEST_BYTES,
            pool_size=PIPE_POOL_SIZE,
            idle_timeout_seconds=PIPE_IDLE_TIMEOUT_SECONDS,
        )
        await self._pipe.start(self._handle_client)

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
        if WINDOWS:
            if self._pipe is None:
                await self.start()
            assert self._pipe is not None
            await self._pipe.serve_forever()
            return
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if WINDOWS:
            if self._pipe is not None:
                await self._pipe.close()
                self._pipe = None
            return
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
        pending: bytes | None = None
        try:
            if WINDOWS:
                # Phase 0B measured that a pipe peer can only be impersonated after a read
                # has completed, so the first frame is read here and buffered: it reaches the
                # dispatcher only once _check_peer has asserted the measured identity.
                pending = await reader.readline()
                if not pending:
                    return
            self._check_peer(writer)
            while True:
                try:
                    raw = pending if pending is not None else await reader.readline()
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
                pending = None
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
        """Assert the identity the endpoint measured for this connection.

        Linux measures SO_PEERCRED here (available at connection setup); Darwin measures
        getpeereid here — the authoritative euid/egid, with no PID fabricated; Windows
        measured during the first read and hands the result over, so this is where the SID
        assertion runs. Either way a failure means nothing was dispatched.
        """
        if WINDOWS:
            assert self._pipe is not None
            authorize_windows_peer(self._pipe.peer(writer), allowed_sid=self.allowed_peer_sid or "")
            return
        sock = writer.get_extra_info("socket")
        if DARWIN:
            authorize_darwin_peer(
                measure_darwin_peer(sock),
                allowed_uid=self.allowed_peer_uid,
                allowed_gid=self.allowed_peer_gid,
            )
            return
        authorize_posix_peer(
            measure_posix_peer(sock),
            allowed_uid=self.allowed_peer_uid,
            allowed_gid=self.allowed_peer_gid,
        )

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
        if method == "runtime.models":
            _require_keys(
                params,
                {"runtime"},
                optional={"workdir", "task_prompt", "path", "profile"},
            )
            _require_types(
                params,
                {"runtime": str},
                optional={
                    "workdir": (str, type(None)),
                    "task_prompt": (str, type(None)),
                    "path": str,
                    "profile": str,
                },
            )
            return await self.service.list_models(**params)
        if method == "task.submit":
            _require_keys(
                params,
                {"runtime", "workdir", "path", "profile", "prompt"},
                optional={"model", "continue_from_task_id", "correlation_id", "idempotency_key"},
            )
            _require_types(
                params,
                {"runtime": str, "workdir": str, "path": str, "profile": str, "prompt": str},
                optional={
                    "model": (str, type(None)),
                    "continue_from_task_id": (str, type(None)),
                    "correlation_id": (str, type(None)),
                    "idempotency_key": (str, type(None)),
                },
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
        if method == "task.result.read":
            _require_keys(params, {"task_id"}, optional={"offset_bytes", "max_bytes"})
            _require_types(
                params,
                {"task_id": str},
                optional={"offset_bytes": int, "max_bytes": int},
            )
            return self.service.read_result(**params)
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
