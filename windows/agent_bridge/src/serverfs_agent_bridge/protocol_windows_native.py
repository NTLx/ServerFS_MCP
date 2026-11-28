"""Windows local Bridge RPC transport; preserve the versioned request and authorization contract."""

from __future__ import annotations

import asyncio
import hmac
import json
import sys
from pathlib import Path
from typing import Any

from .core_grants import CoreAuthorization, CoreGrantVerifier, core_workdir_policy_digest
from .errors import BridgeError
from .local_ipc import PIPE_NAMESPACE, authorize_windows_peer, derive_pipe_name
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
        core_grants: CoreGrantVerifier | None = None,
    ):
        if sys.platform != "win32":
            raise ValueError("Windows Bridge protocol requires Windows")
        self.service = service
        self._pipe: Any = None
        self.allowed_peer_sid = allowed_peer_sid
        self._pipe_name = pipe_name
        # Opt-in v2: when provided, the old unscoped v1 envelope is refused.
        # No production deployment passes grants; private owner configuration
        # and independent v2 Bridge lifecycle are still pending.
        self._core_grants = core_grants
        if socket_path is not None:
            raise ValueError("Windows Bridge requires a Named Pipe endpoint")
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

    async def start(self) -> None:
        if self._core_grants is not None and not self._core_grants.durable_replay_protection:
            raise BridgeError(
                "CORE_REPLAY_STORE_REQUIRED",
                "Bridge protocol v2 requires durable nonce replay protection",
            )
        await self._start_pipe()

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

    async def serve_forever(self) -> None:
        if self._pipe is None:
            await self.start()
        assert self._pipe is not None
        await self._pipe.serve_forever()

    async def close(self) -> None:
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        pending: bytes | None = None
        try:
            # The first frame must be read before the pipe can impersonate its peer.
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
        """Assert the measured SID before dispatching any buffered request."""
        assert self._pipe is not None
        authorize_windows_peer(self._pipe.peer(writer), allowed_sid=self.allowed_peer_sid or "")

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
            authorized: CoreAuthorization | None = None
            if self._core_grants is not None:
                authorized = self._core_grants.authorize(request)
                # A valid MAC over stale or mismatched grant policy cannot
                # authorize access: bind against the current Bridge policy.
                try:
                    policy = self.service.policies.get(authorized.workdir)
                    active_digest = core_workdir_policy_digest(policy)
                except (BridgeError, AttributeError, ValueError) as exc:
                    raise BridgeError(
                        "CORE_NOT_AUTHORIZED", "Core capability is not authorized"
                    ) from exc
                if not hmac.compare_digest(active_digest, request["policy_revision"]):
                    raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            elif not isinstance(request, dict) or set(request) != _ALLOWED_TOP_LEVEL:
                raise BridgeError("INVALID_REQUEST", "request fields do not match protocol")
            if type(request["protocol_version"]) is not int:
                raise BridgeError("INVALID_REQUEST", "protocol_version must be an integer")
            if self._core_grants is None and request["protocol_version"] != PROTOCOL_VERSION:
                raise BridgeError(
                    "PROTOCOL_VERSION_UNSUPPORTED", "unsupported bridge protocol version"
                )
            if not isinstance(request_id, str) or not request_id:
                raise BridgeError("INVALID_REQUEST", "request_id must be a non-empty string")
            method = request["method"]
            params = request["params"]
            if not isinstance(method, str) or not method or not isinstance(params, dict):
                raise BridgeError("INVALID_REQUEST", "method and params have invalid types")
            result = (
                await self._dispatch_core(
                    authorized,
                    params,
                    policy_revision=request["policy_revision"],
                )
                if authorized is not None
                else await self._dispatch(method, params)
            )
            return {"request_id": request_id, "ok": True, "result": result}
        except BridgeError as exc:
            return {"request_id": request_id, "ok": False, "error": exc.to_dict()}
        except Exception:
            return _error_response(request_id, "BRIDGE_PROTOCOL_ERROR", "protocol failure")

    async def _dispatch_core(
        self,
        authorized: CoreAuthorization,
        params: dict[str, Any],
        *,
        policy_revision: str | None = None,
    ) -> dict[str, Any]:
        """v2 request adapter: validate and enforce Core task ownership.

        The legacy v1 dispatch cannot accept Core-selected owner arguments.
        An authenticated v2 submission receives its owner from the verified
        HMAC identity, never from client params. Task-ID-only operations must
        carry a signed workdir; it is removed after a durable owner check,
        before the original strict handler sees the request.
        """
        method = authorized.method
        if method == "bridge.ping":
            # V2 lifecycle probe: Core-key authenticated and bound to the
            # current workdir policy, with no task/runtime disclosure.
            _require_keys(params, {"workdir"})
            _require_types(params, {"workdir": str})
            return {"status": "ok"}
        if method == "runtime.list":
            _require_keys(params, {"workdir"})
            _require_types(params, {"workdir": str})
            policy = self.service.policies.get(authorized.workdir)
            catalog = await self.service.list_runtimes()
            return {
                "runtimes": [
                    runtime
                    for runtime in catalog.get("runtimes", [])
                    if isinstance(runtime, dict) and runtime.get("name") in policy.runtimes
                ]
            }
        if method == "runtime.models":
            _require_keys(
                params,
                {"runtime", "workdir"},
                optional={"task_prompt", "path", "profile"},
            )
            _require_types(
                params,
                {"runtime": str, "workdir": str},
                optional={"task_prompt": str, "path": str, "profile": str},
            )
            policy = self.service.policies.get(authorized.workdir)
            if params["runtime"] not in policy.runtimes:
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            # Runtime-global discovery does not become Jev advice simply
            # because its authorization is signed for a workdir.
            if "task_prompt" not in params:
                if set(params) != {"runtime", "workdir"}:
                    raise BridgeError("INVALID_REQUEST", "model advice requires task_prompt")
                return await self._dispatch(method, {"runtime": params["runtime"]})
            return await self._dispatch(method, params)
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
            return await self.service.submit_task(**params, owner_core_id=authorized.core_id)
        if method == "task.list":
            if policy_revision is None or params.get("workdir") != authorized.workdir:
                raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
            _require_keys(params, {"workdir"}, optional={"status", "limit", "cursor"})
            _require_types(
                params,
                {"workdir": str},
                optional={"status": str, "limit": int, "cursor": str},
            )
            # Discovery is not a task-ID operation. The shared service/store
            # owns scope and pagination validation; only verified identities
            # and the authenticated policy revision cross this boundary.
            return self.service.list_tasks(
                workdir=authorized.workdir,
                core_id=authorized.core_id,
                status=params.get("status", "active"),
                limit=params.get("limit", 20),
                cursor=params.get("cursor"),
                policy_revision=policy_revision,
                grant_key_id=authorized.key_id,
            )
        if method.startswith("task."):
            task_id = params.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise BridgeError("INVALID_REQUEST", "task_id must be a non-empty string")
            self.service.store.require_core_task_owner(
                task_id, core_id=authorized.core_id, workdir_alias=authorized.workdir
            )
            # Only the v2 envelope carries the scoping workdir parameter.
            # Everything else must pass the unchanged v1 strict RPC schema.
            scoped_params = {key: value for key, value in params.items() if key != "workdir"}
            return await self._dispatch(method, scoped_params)
        return await self._dispatch(method, params)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
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
            return await self.service.submit_task(**params)
        if method == "task.list":
            raise BridgeError("CORE_NOT_AUTHORIZED", "Core capability is not authorized")
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
