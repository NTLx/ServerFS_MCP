"""Windows named-Bridge private Agent proxy admission (v0.26.0).

Only the independent Bridge owner loads this material. Provider child
processes receive egress policy exclusively through the Bridge adapters.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from serverfs_mcp.agent_proxy import AgentProxyConfig, AgentProxyError, parse_agent_proxy

_MAX_PROXY_SOURCE_BYTES = 4096
_FIELDS = frozenset({"SERVERFS_AGENT_PROXY_URL", "SERVERFS_AGENT_NO_PROXY"})


class NamedAgentProxyError(ValueError):
    """Redacted refusal to admit an invalid named Agent proxy source."""


def uses_agent_proxy(bridge_request: Mapping[str, object]) -> bool:
    """Whether any explicitly enabled provider/ACP requests proxy egress."""
    runtimes = bridge_request.get("runtimes", {})
    profiles = bridge_request.get("acp_profiles", ())
    if not isinstance(runtimes, dict) or not isinstance(profiles, (list, tuple)):
        raise NamedAgentProxyError("named runtime proxy policy is invalid")
    result = False
    for name, setting in runtimes.items():
        if name not in {"codex", "claude", "qoder"} or not isinstance(setting, dict):
            raise NamedAgentProxyError("named runtime proxy policy is invalid")
        flag = setting.get("use_proxy", False)
        if type(flag) is not bool:
            raise NamedAgentProxyError("named runtime proxy policy is invalid")
        if flag and setting.get("enabled") is not True:
            raise NamedAgentProxyError("disabled runtime cannot request a proxy")
        result |= flag
    for profile in profiles:
        if not isinstance(profile, dict) or type(profile.get("use_proxy", False)) is not bool:
            raise NamedAgentProxyError("named ACP proxy policy is invalid")
        result |= profile.get("use_proxy", False)
    return result


def parse_proxy_source(raw: bytes) -> AgentProxyConfig:
    """Parse a bounded dedicated key/value file using the existing proxy validator.

    The caller independently validates the private ACL and rejects reparse paths.
    No dotenv expansion, substitution, includes, or proxy authentication.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_PROXY_SOURCE_BYTES:
        raise NamedAgentProxyError("named proxy input is unavailable or oversized")
    try:
        text = raw.decode("utf-8", "strict").replace("\r\n", "\n")
        if "\r" in text or "\x00" in text:
            raise NamedAgentProxyError("named proxy input is invalid")
        entries: dict[str, str] = {}
        for line in text.split("\n"):
            if not line:
                continue
            if "=" not in line:
                raise NamedAgentProxyError("named proxy input is invalid")
            key, value = line.split("=", 1)
            if key not in _FIELDS or key in entries or not value:
                raise NamedAgentProxyError("named proxy input is invalid")
            entries[key] = value
        if "SERVERFS_AGENT_PROXY_URL" not in entries:
            raise NamedAgentProxyError("named Agent proxy endpoint is required")
        value = parse_agent_proxy(SimpleNamespace(enabled=True, source="env"), entries)
        if value is None:
            raise NamedAgentProxyError("named Agent proxy endpoint is required")
        return value
    except (UnicodeError, AgentProxyError, ValueError):
        raise NamedAgentProxyError("named Agent proxy input is invalid") from None


def load_named_agent_proxy(
    *, bridge_home: Path, sid: str, bridge_document: Mapping[str, object]
) -> AgentProxyConfig | None:
    """Load a protected owner-private endpoint only when policy explicitly opts in."""
    if not isinstance(bridge_document, dict):
        raise NamedAgentProxyError("named Bridge proxy policy is invalid")
    request = {
        "runtimes": {
            key: bridge_document[key]
            for key in ("codex", "claude", "qoder")
            if key in bridge_document
        },
        "acp_profiles": bridge_document.get("acp_profiles", []),
    }
    if not uses_agent_proxy(request):
        return None
    from serverfs_mcp.windows_v25 import storage

    path = bridge_home / "agent-proxy.env"
    try:
        storage.check_private(path, directory=False, sid=sid)
        info = path.stat(follow_symlinks=False)
        if not 0 < info.st_size <= _MAX_PROXY_SOURCE_BYTES:
            raise NamedAgentProxyError("named proxy input is unavailable or oversized")
        return parse_proxy_source(path.read_bytes())
    except (OSError, ValueError):
        raise NamedAgentProxyError("named Bridge proxy input is unavailable") from None


def read_owned_named_agent_proxy(*, manifest, config_path: Path) -> AgentProxyConfig | None:
    """Reopen owner-private configuration and admit only its requested proxy."""
    from serverfs_mcp.windows_v25 import storage

    home = storage.home("bridge", manifest.instance_id)
    try:
        storage.check_private(config_path, directory=False, sid=manifest.sid)
        if not 0 < config_path.stat().st_size <= 256 * 1024:
            raise NamedAgentProxyError("named Bridge document is invalid")

        def unique_pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise NamedAgentProxyError("named Bridge document is ambiguous")
                result[key] = value
            return result

        document = json.loads(config_path.read_text("utf-8"), object_pairs_hook=unique_pairs)
        return load_named_agent_proxy(bridge_home=home, sid=manifest.sid, bridge_document=document)
    except (OSError, ValueError, UnicodeError, TypeError):
        raise NamedAgentProxyError("named Bridge private proxy could not be verified") from None
