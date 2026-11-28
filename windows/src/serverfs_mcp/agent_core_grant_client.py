"""Opt-in Bridge protocol-v2 Core client, NOT connected to any service.

Every request carries a per-Core HMAC proof over its complete Bridge method,
arguments and current operator-pinned workdir policy digest. The existing
AgentBridgeClient remains protocol v1 with identical behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .agent_client import (
    MAX_REQUEST_BYTES,
    AgentBridgeClient,
    AgentBridgeClientError,
    AgentBridgeRemoteError,
    AgentBridgeUnavailable,
    validate_signed_v2_submission,
)

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
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


_TASK_BY_ID = frozenset(
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


class CoreGrantBridgeClient(AgentBridgeClient):
    """Independent v2 identity, no fallback to unscoped v1 Bridge methods."""

    def __init__(
        self,
        socket_path: Path,
        *,
        core_id: str,
        bridge_id: str,
        key_id: str,
        secret: bytes,
        policy_revisions: Mapping[str, str],
        timeout_seconds: float = 15.0,
        server_pid_validator: Callable[[int], bool] | None = None,
    ):
        # Same-SID named-pipe ownership is not proof of the selected Bridge.
        # An independently managed v2 Windows Bridge must be pinned to an
        # independently verified Task/Job process generation by its caller.
        if server_pid_validator is None:
            raise ValueError("Windows Bridge v2 requires an owner-generation validator")
        super().__init__(
            socket_path,
            timeout_seconds=timeout_seconds,
            server_pid_validator=server_pid_validator,
        )
        if any(
            not isinstance(value, str) or not _ID.fullmatch(value)
            for value in (core_id, bridge_id, key_id)
        ):
            raise ValueError("invalid Core/Bridge capability identity")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("Core capability needs a 256-bit private secret")
        if not isinstance(policy_revisions, Mapping) or not policy_revisions:
            raise ValueError("Core capability must bind at least one workdir policy")
        digest_map = dict(policy_revisions)
        if len(digest_map) > 16:
            raise ValueError("Bridge v2 Core capability exceeds workdir grant limit")
        for alias, digest in digest_map.items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError("invalid Bridge workdir alias")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("invalid Bridge policy digest")
        self.core_id = core_id
        self.bridge_id = bridge_id
        self.key_id = key_id
        self._secret = secret
        self._policy_revisions = MappingProxyType(digest_map)

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Preserve the v1 public MCP tool arguments without dropping v2 scope.

        Existing task tools expose a task_id without a workdir. Resolve it
        through bounded, HMAC-signed, per-workdir attempts. The Bridge checks
        immutable Core+workdir task ownership BEFORE any side effect. Unknown
        or foreign task IDs cannot be inferred through these attempts.
        """
        if not isinstance(method, str) or method not in _METHODS:
            raise AgentBridgeClientError("Bridge v2 method is not authorized")
        if not isinstance(params, dict):
            raise AgentBridgeClientError("Bridge v2 requires valid parameters")
        if method == "task.list" and not isinstance(params.get("workdir"), str):
            raise AgentBridgeClientError("Bridge v2 task discovery requires a named workdir")
        if method == "runtime.list" and not params:
            names: set[str] = set()
            runtimes: list[dict[str, Any]] = []
            successful = False
            for workdir in self._policy_revisions:
                try:
                    catalog = await self._signed_call(method, {"workdir": workdir})
                except AgentBridgeRemoteError as exc:
                    if exc.code == "CORE_NOT_AUTHORIZED":
                        continue
                    raise
                entries = catalog.get("runtimes")
                if not isinstance(entries, list) or any(
                    not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                    for entry in entries
                ):
                    raise AgentBridgeClientError("Bridge v2 returned an invalid runtime catalog")
                successful = True
                for entry in entries:
                    name = entry["name"]
                    if name not in names:
                        names.add(name)
                        runtimes.append(entry)
            if not successful:
                raise AgentBridgeClientError("Bridge v2 runtime discovery is not authorized")
            return {"runtimes": runtimes}
        if "workdir" not in params and (method == "runtime.models" or method in _TASK_BY_ID):
            if method in _TASK_BY_ID and (
                not isinstance(params.get("task_id"), str) or not params["task_id"]
            ):
                raise AgentBridgeClientError("Bridge v2 requires a task ID")
            if method == "runtime.models" and not isinstance(params.get("runtime"), str):
                raise AgentBridgeClientError("Bridge v2 requires a runtime")
            for workdir in self._policy_revisions:
                try:
                    return await self._signed_call(method, {**params, "workdir": workdir})
                except AgentBridgeRemoteError as exc:
                    if exc.code in ("CORE_NOT_AUTHORIZED", "CORE_TASK_NOT_FOUND"):
                        continue
                    raise
            raise AgentBridgeClientError(
                "Bridge v2 Core capability does not authorize this request"
            )
        return await self._signed_call(method, params)

    async def _signed_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One signed request; never sends an unscoped or v1 fallback RPC."""
        if not isinstance(params.get("workdir"), str):
            raise AgentBridgeClientError("Bridge v2 requires a named workdir")
        validate_signed_v2_submission(method, params)
        policy_digest = self._policy_revisions.get(params["workdir"])
        if policy_digest is None:
            raise AgentBridgeClientError("Bridge v2 workdir is not authorized")

        request_id = f"mcp_{secrets.token_hex(12)}"
        unsigned = {
            "protocol_version": 2,
            "bridge_id": self.bridge_id,
            "core_id": self.core_id,
            "key_id": self.key_id,
            "request_id": request_id,
            "method": method,
            "params": params,
            "policy_revision": policy_digest,
            "issued_at": int(time.time()),
            "nonce": secrets.token_hex(16),
        }
        try:
            message = json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            signature = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
            encoded = (
                json.dumps(
                    {**unsigned, "signature": signature},
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise AgentBridgeClientError("invalid signed Bridge v2 request payload") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AgentBridgeClientError("Bridge v2 request exceeds the size limit")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._exchange(encoded, request_id)
        except AgentBridgeRemoteError:
            raise
        except TimeoutError as exc:
            raise AgentBridgeUnavailable("Bridge v2 request timed out") from exc
        except (ConnectionError, OSError) as exc:
            raise AgentBridgeUnavailable("Bridge v2 endpoint is unavailable") from exc
        except ValueError as exc:
            raise AgentBridgeClientError("Bridge v2 returned an invalid response") from exc
