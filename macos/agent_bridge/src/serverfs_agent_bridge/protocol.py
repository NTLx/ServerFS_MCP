"""macOS local Bridge RPC transport; preserve the versioned request and authorization contract."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from .core_grants import CoreGrantVerifier
from .errors import BridgeError
from .local_ipc import authorize_darwin_peer, max_socket_path_bytes, measure_darwin_peer
from .service import BridgeService

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
_ALLOWED_TOP_LEVEL = frozenset({"protocol_version", "request_id", "method", "params"})
_V2_TASK_METHODS = frozenset(
    {
        "task.get",
        "task.events",
        "task.result.read",
        "task.cancel",
        "task.message.send",
        "task.approval.respond",
        "task.question.answer",
    }
)
_V2_DISPATCH_METHODS = frozenset(
    {
        "bridge.ping",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
        "runtime.list",
        "runtime.models",
        "task.submit",
        "task.list",
        *_V2_TASK_METHODS,
    }
)


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
        v2_only: bool = False,
        core_grant_verifier: CoreGrantVerifier | None = None,
    ):
        if sys.platform != "darwin":
            raise ValueError("macOS Bridge protocol requires macOS")
        self.service = service
        self.socket_path = socket_path
        self._shared_gid_explicit = allowed_peer_gid is not None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self.allowed_peer_uid = allowed_peer_uid
        self.allowed_peer_gid = allowed_peer_gid
        if type(v2_only) is not bool:
            raise ValueError("v2_only must be a boolean")
        if v2_only and core_grant_verifier is None:
            raise ValueError("v2-only protocol requires a Core grant verifier")
        if not v2_only and core_grant_verifier is not None:
            raise ValueError("Core grant verifier requires v2-only protocol mode")
        self.v2_only = v2_only
        self.core_grant_verifier = core_grant_verifier
        if allowed_peer_sid is not None or pipe_name is not None:
            raise ValueError("this Bridge requires a Unix socket endpoint")
        if socket_path is None:
            raise ValueError("socket_path must be set where the local endpoint is a socket")
        if not self.socket_path.is_absolute():
            raise ValueError("socket_path must be absolute")
        if allowed_peer_uid is None and allowed_peer_gid is None:
            self.allowed_peer_uid = os.getuid()
            self.allowed_peer_gid = os.getgid()

    async def start(self) -> None:
        if self.v2_only and not self.core_grant_verifier.durable_replay_protection:
            raise BridgeError(
                "CORE_NOT_AUTHORIZED", "v2 protocol requires durable replay protection"
            )
        if len(os.fsencode(self.socket_path)) > max_socket_path_bytes():
            raise BridgeError(
                "SOCKET_PATH_TOO_LONG", "socket path exceeds the Darwin sun_path limit"
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
        """Authorize the measured getpeereid identity before dispatch."""
        authorize_darwin_peer(
            measure_darwin_peer(writer.get_extra_info("socket")),
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
            if self.v2_only:
                return await self._dispatch_v2(request, request_id)
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

    async def _dispatch_v2(self, request: object, request_id: str | None) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
        if request.get("protocol_version") != 2 or type(request.get("protocol_version")) is not int:
            raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
        authorization = self.core_grant_verifier.authorize(request)
        method = authorization.method
        if method not in _V2_DISPATCH_METHODS:
            raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
        params = dict(request["params"])
        # The canonical Core envelope binds a workdir for every method. Legacy RPC
        # methods keep their existing parameter shape after that scope is checked.
        if method in _V2_TASK_METHODS:
            if not isinstance(params.get("task_id"), str):
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            self.service.store.require_core_task_owner(
                params["task_id"],
                core_id=authorization.core_id,
                workdir_alias=authorization.workdir,
            )
            params.pop("workdir")
        elif method in {"bridge.ping", "runtime.list"}:
            params.pop("workdir")
        elif method == "task.submit":
            if params.get("workdir") != authorization.workdir:
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
        elif method == "task.list":
            params.pop("workdir")
        result = await self._dispatch(
            method,
            params,
            owner_core_id=authorization.core_id if method == "task.submit" else None,
            task_list_core_id=authorization.core_id if method == "task.list" else None,
            task_list_workdir=authorization.workdir if method == "task.list" else None,
            policy_revision=request["policy_revision"] if method == "task.list" else None,
            task_list_grant_key_id=authorization.key_id if method == "task.list" else None,
        )
        return {"request_id": request_id, "ok": True, "result": result}

    async def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        *,
        owner_core_id: str | None = None,
        task_list_core_id: str | None = None,
        task_list_workdir: str | None = None,
        policy_revision: str | None = None,
        task_list_grant_key_id: str | None = None,
    ) -> dict[str, Any]:
        if method == "diagnostics.snapshot":
            _require_keys(params, set())
            return self.service.diagnostics_snapshot()
        if method == "diagnostics.task":
            _require_keys(params, {"task_id"})
            _require_types(params, {"task_id": str})
            try:
                task_id_bytes = len(params["task_id"].encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise BridgeError("INVALID_REQUEST", "task_id must be valid UTF-8") from exc
            if not params["task_id"] or task_id_bytes > 256:
                raise BridgeError("INVALID_REQUEST", "task_id must be a non-empty bounded string")
            return self.service.diagnostics_task(params["task_id"])
        if method == "bridge.ping":
            # Internal liveness RPC for the supervisor watchdog. Handled here, never in
            # BridgeService: a wedged event loop answers nothing, which is exactly the
            # signal the watchdog needs. No runtime, provider, discovery or network work.
            _require_keys(params, set())
            return {"status": "ok"}
        if method == "repo.inspect":
            _require_keys(params, {"workdir", "view"}, optional={"paths"})
            _require_types(params, {"workdir": str, "view": str}, optional={"paths": list})
            paths = params.get("paths", [])
            if any(not isinstance(item, str) for item in paths):
                raise BridgeError("INVALID_REQUEST", "repo paths must be strings")
            return await self.service.inspect_repo(
                workdir=params["workdir"],
                view=params["view"],
                paths=paths,
            )
        if method == "repo.action":
            _require_keys(params, {"workdir", "action"}, optional={"path", "release_tag"})
            _require_types(
                params,
                {"workdir": str, "action": str},
                optional={"path": (str, type(None)), "release_tag": (str, type(None))},
            )
            release_params = {}
            if params.get("release_tag") is not None:
                release_params["release_tag"] = params["release_tag"]
            return await self.service.run_repo_action(
                workdir=params["workdir"],
                action=params["action"],
                path=params.get("path"),
                **release_params,
            )
        if method == "repo.commit":
            _require_keys(params, {"workdir", "paths", "message"})
            _require_types(params, {"workdir": str, "paths": list, "message": str})
            if any(not isinstance(item, str) for item in params["paths"]):
                raise BridgeError("INVALID_REQUEST", "repo paths must be strings")
            return await self.service.commit_repo_changes(
                workdir=params["workdir"],
                paths=params["paths"],
                message=params["message"],
            )
        if method == "repo.push":
            _require_keys(params, {"workdir"})
            _require_types(params, {"workdir": str})
            return await self.service.push_repo(workdir=params["workdir"])
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
            if owner_core_id is not None:
                return await self.service.submit_task(**params, owner_core_id=owner_core_id)
            return await self.service.submit_task(**params)
        if method == "task.list":
            if task_list_core_id is None or policy_revision is None:
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            _require_keys(params, set(), optional={"status", "limit", "cursor"})
            _require_types(
                params,
                {},
                optional={"status": str, "limit": int, "cursor": str},
            )
            if task_list_workdir is None:
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            return self.service.list_tasks(
                workdir=task_list_workdir,
                core_id=task_list_core_id,
                status=params.get("status", "active"),
                limit=params.get("limit", 20),
                cursor=params.get("cursor"),
                policy_revision=policy_revision,
                grant_key_id=task_list_grant_key_id or "",
            )
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
                {"task_id", "request_id"},
                optional={"decision", "option_id", "granted_permission_ids"},
            )
            _require_types(
                params,
                {"task_id": str, "request_id": str},
                optional={"decision": str, "option_id": str, "granted_permission_ids": list},
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
