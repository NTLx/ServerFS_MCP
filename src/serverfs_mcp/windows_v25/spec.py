"""Pure fixed manifests for the four independent Windows v0.25 services."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serverfs_mcp.instance_identity import (
    DEFAULT_INSTANCE_ID,
    InstanceIdentity,
    InstanceIdentityError,
    valid_id,
)

SCHEMA = 1
_SID = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")
_TUNNEL = re.compile(r"tunnel_[a-z0-9]{32}\Z")
COMPONENTS = {
    "core": "ServerFS v25 Core",
    "bridge": "ServerFS v25 Agent Bridge",
    "cloudflare": "ServerFS v25 Cloudflare Connector",
    "openai": "ServerFS v25 OpenAI Connector",
}
_FIELDS = {
    "core": frozenset({"config", "auth_file", "port", "hostname"}),
    "bridge": frozenset({"config", "bridge_python"}),
    "cloudflare": frozenset({"cloudflared", "token_file", "port", "hostname"}),
    "openai": frozenset(
        {
            "tunnel_client",
            "tunnel_id",
            "control_plane_key_file",
            "core_auth_header_file",
            "port",
            "hostname",
        }
    ),
}
_PATHS = frozenset(
    {
        "config",
        "auth_file",
        "bridge_python",
        "cloudflared",
        "token_file",
        "tunnel_client",
        "control_plane_key_file",
        "core_auth_header_file",
    }
)


class ManifestError(ValueError):
    """Non-secret component schema or identity refusal."""


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ManifestError("service setting is not an absolute path")
    if ".." in Path(value).parts:
        raise ManifestError("parent-relative service path is forbidden")
    return value


def _host(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ManifestError("Core Host is invalid")
    if not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ManifestError("Core Host is invalid")
    return value


@dataclass(frozen=True)
class Manifest:
    component: str
    sid: str
    pythonw: str
    settings: dict[str, str | int]
    schema: int = SCHEMA
    instance_id: str = DEFAULT_INSTANCE_ID
    connector_id: str | None = None

    @classmethod
    def parse(cls, raw: object) -> Manifest:
        if not isinstance(raw, dict):
            raise ManifestError("service manifest shape is invalid")
        schema = raw.get("schema")
        if type(schema) is not int or schema not in (SCHEMA, 2):
            raise ManifestError("service manifest version is invalid")
        fields = {"schema", "component", "sid", "pythonw", "settings"}
        if schema == 2:
            fields.add("instance_id")
            if "connector_id" in raw:
                fields.add("connector_id")
        if set(raw) != fields:
            raise ManifestError("service manifest shape is invalid")
        instance_id = raw.get("instance_id", DEFAULT_INSTANCE_ID)
        try:
            valid_id(instance_id)
        except InstanceIdentityError as exc:
            raise ManifestError("invalid service instance ID") from exc
        if (schema == 1 and instance_id != DEFAULT_INSTANCE_ID) or (
            schema == 2 and instance_id == DEFAULT_INSTANCE_ID
        ):
            raise ManifestError("default installation requires v1 manifest")
        component = raw["component"]
        if not isinstance(component, str) or component not in COMPONENTS:
            raise ManifestError("service component is unknown")
        connector_id = raw.get("connector_id")
        if connector_id is not None:
            if schema != 2 or component not in ("cloudflare", "openai"):
                raise ManifestError("only independent connectors may have connector IDs")
            try:
                valid_id(connector_id)
            except InstanceIdentityError as exc:
                raise ManifestError("invalid Connector ID") from exc
        elif "connector_id" in raw:
            raise ManifestError("Connector ID cannot be null")
        sid = raw["sid"]
        if not isinstance(sid, str) or not _SID.fullmatch(sid):
            raise ManifestError("service user SID is invalid")
        pythonw = _path(raw["pythonw"])
        if Path(pythonw).name.casefold() != "pythonw.exe":
            raise ManifestError("service launcher must be pythonw.exe")
        settings = raw["settings"]
        if not isinstance(settings, dict) or set(settings) != _FIELDS[component]:
            raise ManifestError("service settings shape is invalid")
        approved: dict[str, str | int] = {}
        for key, value in settings.items():
            if key in _PATHS:
                approved[key] = _path(value)
            elif key == "port":
                if type(value) is not int or not 1 <= value <= 65535:
                    raise ManifestError("Core loopback port is invalid")
                approved[key] = value
            elif key == "hostname":
                approved[key] = _host(value)
            elif key == "tunnel_id":
                if not isinstance(value, str) or not _TUNNEL.fullmatch(value):
                    raise ManifestError("Tunnel ID is invalid")
                approved[key] = value
            else:
                raise ManifestError("unexpected component setting")
        if (
            component == "bridge"
            and Path(str(approved["bridge_python"])).name.casefold() != "python.exe"
        ):
            raise ManifestError("Bridge runtime interpreter is invalid")
        if (
            component == "cloudflare"
            and Path(str(approved["cloudflared"])).name.casefold() != "cloudflared.exe"
        ):
            raise ManifestError("Cloudflare executable is invalid")
        return cls(component, sid, pythonw, approved, schema, instance_id, connector_id)

    def task_argv(self) -> tuple[str, ...]:
        # No runtime-supplied shell/argv/credentials or workdir arguments.
        base = ("-I", "-B", "-m", "serverfs_mcp.windows_v25.runtime", self.component)
        if self.instance_id == DEFAULT_INSTANCE_ID:
            return base
        return (
            (*base, self.instance_id)
            if self.connector_id is None
            else (*base, self.instance_id, self.connector_id)
        )

    def task_arguments(self) -> str:
        return subprocess.list2cmdline(self.task_argv())

    def task_name(self) -> str:
        # Route the legacy/default installation through the common instance
        # identity contract; no existing Task Scheduler name may change.
        return InstanceIdentity(self.instance_id).windows_task(
            self.component, connector_id=self.connector_id
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "component": self.component,
            "sid": self.sid,
            "pythonw": self.pythonw,
            "settings": dict(self.settings),
        }
        if self.schema == 2:
            payload["instance_id"] = self.instance_id
            if self.connector_id is not None:
                payload["connector_id"] = self.connector_id
        return payload
