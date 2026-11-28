"""Pure ServerFS v0.25 instance identity and OS-resource naming contract.

Naming is not authorization or runtime collision detection. Existing default
installations retain their exact OS identities. No service is modified here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DEFAULT_INSTANCE_ID = "default"
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_COMPONENTS = frozenset({"core", "bridge", "cloudflare", "openai"})
_WINDOWS_BASE = {
    "core": "ServerFS v25 Core",
    "bridge": "ServerFS v25 Agent Bridge",
    "cloudflare": "ServerFS v25 Cloudflare Connector",
    "openai": "ServerFS v25 OpenAI Connector",
}
_WINDOWS_SUFFIX = {
    "core": "Core",
    "bridge": "Agent Bridge",
    "cloudflare": "Cloudflare Connector",
    "openai": "OpenAI Connector",
}
_MAC_DEFAULT = {
    "core": "com.ntlx.serverfs.http-origin",
    "bridge": "com.ntlx.serverfs.bridge",
    "cloudflare": "com.ntlx.serverfs.cloudflare",
    "openai": "com.ntlx.serverfs.tunnel",
}


class InstanceIdentityError(ValueError):
    """A requested service/instance identity is unsafe or inconsistent."""


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InstanceIdentityError("invalid ServerFS instance identifier")
    return value


def _component(value: str) -> str:
    if not isinstance(value, str) or value not in _COMPONENTS:
        raise InstanceIdentityError("unknown ServerFS component")
    return value


@dataclass(frozen=True)
class InstanceIdentity:
    """A Core installation and its optional Bridge binding.

    mode=owned: this installation owns a private Bridge named as itself.
    mode=shared: an already managed Bridge owns its own lifetime.
    mode=disabled: no Bridge client or service is configured.
    """

    instance_id: str = DEFAULT_INSTANCE_ID
    bridge_mode: Literal["disabled", "owned", "shared"] = "disabled"
    bridge_id: str | None = None

    def __post_init__(self) -> None:
        valid_id(self.instance_id)
        if self.bridge_mode == "disabled":
            if self.bridge_id is not None:
                raise InstanceIdentityError("disabled Bridge cannot have a reference")
        elif self.bridge_mode == "owned":
            if self.bridge_id != self.instance_id:
                raise InstanceIdentityError("owned Bridge identity must equal instance ID")
        elif self.bridge_mode == "shared":
            if self.bridge_id is None:
                raise InstanceIdentityError("shared Bridge must have a reference")
            valid_id(self.bridge_id)
        else:
            raise InstanceIdentityError("invalid Bridge mode")

    def windows_task(self, component: str, connector_id: str | None = None) -> str:
        """Stable current default task; deterministic distinct future tasks."""
        _component(component)
        if connector_id is not None:
            if component not in ("cloudflare", "openai"):
                raise InstanceIdentityError("only ingress components take connector IDs")
            valid_id(connector_id)
        if self.instance_id == DEFAULT_INSTANCE_ID and connector_id is None:
            return _WINDOWS_BASE[component]
        name = f"ServerFS v25 {self.instance_id} {_WINDOWS_SUFFIX[component]}"
        return name if connector_id is None else f"{name} {connector_id}"

    def macos_label(self, component: str, connector_id: str | None = None) -> str:
        _component(component)
        if connector_id is not None:
            if component not in ("cloudflare", "openai"):
                raise InstanceIdentityError("only ingress components take connector IDs")
            valid_id(connector_id)
        if self.instance_id == DEFAULT_INSTANCE_ID and connector_id is None:
            return _MAC_DEFAULT[component]
        suffix = {
            "core": "core",
            "bridge": "bridge",
            "cloudflare": "cloudflare",
            "openai": "openai",
        }[component]
        name = f"com.ntlx.serverfs.{self.instance_id}.{suffix}"
        return name if connector_id is None else f"{name}.{connector_id}"

    def windows_data_relative(self) -> str:
        """Relative to %LOCALAPPDATA%/ServerFS, never a user-supplied path."""
        if self.instance_id == DEFAULT_INSTANCE_ID:
            return "v25"
        return f"v25/instances/{self.instance_id}"

    def macos_data_relative(self) -> str:
        """Relative to ~/Library/Application Support/ServerFS."""
        if self.instance_id == DEFAULT_INSTANCE_ID:
            return "service-manager"
        return f"instances/{self.instance_id}/service-manager"

    def linux_compose_project(self) -> str:
        """New deployment naming only: existing Compose project is untouched."""
        return f"serverfs-{self.instance_id}"

    def linux_bridge_unit(self) -> str:
        if self.bridge_mode == "disabled":
            raise InstanceIdentityError("no Bridge for file-only instance")
        assert self.bridge_id is not None
        if self.bridge_id == DEFAULT_INSTANCE_ID:
            return "serverfs-agent-bridge.service"
        return f"serverfs-agent-bridge@{self.bridge_id}.service"
