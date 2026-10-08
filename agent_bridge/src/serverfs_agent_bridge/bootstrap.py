"""The private supervised bootstrap channel (v0.11 Phase D, §15 D4).

A supervised Bridge receives its **runtime-only** material over its own stdin, as one bounded JSON
frame, and then keeps that pipe open as the shutdown control channel.

Why stdin and not the alternatives — this is the load-bearing decision of the phase:

The material in question is the Agent egress proxy endpoint. Phase 0F measured that this value must
not be reachable from anything the provider SDKs inherit, and both SDKs copy the **whole** process
environment into their CLI child. So every alternative was worse by measurement, not by taste:

- **argv** — visible to any same-user process that can open the Bridge process, and argv is not
  redacted by anything. §15 D2 also requires an argv secret scan.
- **the Bridge's own environment** — inherited wholesale into the provider child, and from there
  into every tool the Agent runs. This is the exact leak Phase 0F §7.1 measured.
- **the generated JSON config** — persisted private state. §15 D2 requires the generated document
  to contain no proxy material, and a test asserts the absence.
- **registry / temp plaintext** — a second copy with a longer life than the process that uses it.

stdin is parent-to-child, unpersisted, and never inherited by a grandchild. The value therefore
exists in exactly two places: the supervisor's memory and this process's memory.

What this frame is **not**: it is not the MCP protocol, not the public Agent Bridge RPC, and it does
not touch ``PROTOCOL_VERSION``. It is a private parent-to-child lifecycle channel with exactly one
frame and no reply, so adding it changes no public surface (§15 D4, §70).

Parser rules (§15 D4): bounded size, strict JSON, known keys only, exactly one frame, and fail
closed on anything malformed. The proxy value is never logged, never echoed and never persisted —
it lives only in the returned in-memory object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: One bootstrap frame is runtime-only configuration. The bound is deliberately far below any real
#: frame so an oversized payload is refused instead of buffered.
MAX_BOOTSTRAP_BYTES = 64 * 1024

#: The frame version, independent of the public RPC PROTOCOL_VERSION: this channel carries no
#: protocol semantics and must not be mistaken for one.
BOOTSTRAP_VERSION = 1

_FRAME_KEYS = frozenset({"version", "agent_proxy", "prior_bridge_execution_stopped"})
_PROXY_KEYS = frozenset({"enabled", "url", "no_proxy"})


class BootstrapError(Exception):
    """The bootstrap frame is unusable; the Bridge must refuse to start."""


@dataclass(frozen=True)
class RuntimeProxy:
    """Runtime-only egress material, held in memory for the life of the process.

    ``repr=False`` on the endpoint so it cannot reach a transcript through an exception message or a
    debug log. There is deliberately no serializer on this type: nothing here is ever written back
    out, which is what makes "never persisted" a property rather than a promise.
    """

    url: str
    no_proxy: str

    @property
    def canonical_url(self) -> str:
        """The credentialless endpoint, shaped for a provider child's HTTPS_PROXY."""
        return self.url

    def __repr__(self) -> str:
        return "RuntimeProxy(redacted=True)"


@dataclass(frozen=True)
class BootstrapFrame:
    """One parsed bootstrap frame."""

    version: int
    agent_proxy: RuntimeProxy | None
    #: Whether the previous generation's *execution* has been stopped: the supervisor holds the
    #: per-user lifecycle lease and *created* the SID-scoped named Job Object rather than finding
    #: one, so the object that held the old provider tree is gone and Windows has delivered
    #: termination to every member. The old provider cannot keep running Agent or tool code.
    #: This is deliberately weaker than "every process object has been destroyed": termination is
    #: asynchronous on Windows, and teardown of objects and pending I/O can trail by a fraction of
    #: a millisecond (measured). Not a secret, but a private parent-to-child lifecycle statement:
    #: not part of the public RPC, does not touch PROTOCOL_VERSION, and defaults to False so an
    #: unsupervised launch (or an older supervisor) stays fail-closed.
    prior_bridge_execution_stopped: bool = False

    @property
    def has_proxy(self) -> bool:
        return self.agent_proxy is not None


def _strict_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BootstrapError(f"bootstrap {key} must be a non-empty string")
    return value


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        # Named by count only: an unknown key could itself be a secret-shaped key, and echoing key
        # names into a log line is a small leak surface this module does not need.
        raise BootstrapError(f"bootstrap {label} has {len(unknown)} unknown field(s)")


def parse_bootstrap_frame(raw: bytes) -> BootstrapFrame:
    """Parse one bounded bootstrap frame, or fail closed.

    Strict on every axis: size, JSON wellformedness, frame version, known keys only, and the shape
    of the proxy block. A malformed frame aborts startup rather than degrading to "no proxy",
    because a Bridge silently running without the egress it was told to use would leak provider
    traffic direct with no indication.
    """
    if len(raw) > MAX_BOOTSTRAP_BYTES:
        raise BootstrapError("bootstrap frame exceeds the bounded size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError("bootstrap frame is not valid UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BootstrapError("bootstrap frame is not valid JSON") from exc
    if not isinstance(data, dict):
        raise BootstrapError("bootstrap frame must be a JSON object")
    _reject_unknown(data, _FRAME_KEYS, "frame")

    version = data.get("version")
    if version != BOOTSTRAP_VERSION:
        raise BootstrapError("bootstrap frame version is not supported")

    # Optional and default-False: an absent key means "not proven", so an unsupervised launch or an
    # older supervisor keeps the fail-closed behaviour rather than being read as containment.
    stopped = data.get("prior_bridge_execution_stopped", False)
    if type(stopped) is not bool:
        raise BootstrapError("bootstrap prior_bridge_execution_stopped must be a boolean")

    proxy_block = data.get("agent_proxy")
    if proxy_block is None:
        return BootstrapFrame(
            version=BOOTSTRAP_VERSION, agent_proxy=None, prior_bridge_execution_stopped=stopped
        )
    if not isinstance(proxy_block, dict):
        raise BootstrapError("bootstrap agent_proxy must be an object")
    _reject_unknown(proxy_block, _PROXY_KEYS, "agent_proxy")
    enabled = proxy_block.get("enabled")
    if type(enabled) is not bool:
        raise BootstrapError("bootstrap agent_proxy.enabled must be a boolean")
    if not enabled:
        # An explicitly disabled proxy is still a configuration statement, so it is accepted and
        # recorded as "no proxy" rather than treated as malformed.
        return BootstrapFrame(
            version=BOOTSTRAP_VERSION, agent_proxy=None, prior_bridge_execution_stopped=stopped
        )
    return BootstrapFrame(
        version=BOOTSTRAP_VERSION,
        agent_proxy=RuntimeProxy(
            url=_strict_str(proxy_block, "url"),
            no_proxy=_strict_str(proxy_block, "no_proxy"),
        ),
        prior_bridge_execution_stopped=stopped,
    )


def encode_bootstrap_frame(
    proxy: RuntimeProxy | None, *, prior_bridge_execution_stopped: bool = False
) -> bytes:
    """Build the frame the supervisor writes. Kept beside the parser so the two cannot drift."""
    document: dict[str, Any] = {"version": BOOTSTRAP_VERSION}
    if prior_bridge_execution_stopped:
        # Written only when true, so a frame from a path that cannot prove containment looks exactly
        # like an older supervisor's and is read as "not proven".
        document["prior_bridge_execution_stopped"] = True
    if proxy is not None:
        document["agent_proxy"] = {
            "enabled": True,
            "url": proxy.url,
            "no_proxy": proxy.no_proxy,
        }
    else:
        document["agent_proxy"] = {"enabled": False}
    return json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"


__all__ = [
    "BOOTSTRAP_VERSION",
    "MAX_BOOTSTRAP_BYTES",
    "BootstrapError",
    "BootstrapFrame",
    "RuntimeProxy",
    "encode_bootstrap_frame",
    "parse_bootstrap_frame",
]
