"""Agent runtime egress proxy policy (v0.11 Phase D, §7.1/§7.2, Phase 0F).

This module is the whole of the Agent proxy product surface, and it exists as a narrow
independent unit because the proxy is a **trust-domain boundary**, not a convenience setting.

The two rules everything else follows from, both measured in Phase 0F:

1. **The Bridge process environment must carry no Tunnel/Control Plane credential and no proxy
   variable at all.** Both provider SDKs build their CLI child environment by copying the *whole*
   Bridge environment, and Claude's SDK cannot unset an inherited variable. So a credential that
   reaches the Bridge reaches agent-executed tool code, and a proxy variable set "just for Jev"
   leaks into every provider child. Prevention therefore happens here, when the supervisor builds
   the Bridge environment — not later, at spawn time.
2. **Per-runtime proxy policy is applied strictly downward**, into the environment of the one
   network-owning child. ``use_proxy=false`` means genuinely proxy-free, even when the parent
   process has proxy variables set and even when an endpoint is configured.

Nothing here writes to ``os.environ``, and nothing here mutates process-wide state. That is a
contract, not a style preference: a module that set ``HTTPS_PROXY`` to make one feature work would
break rule 1 for every provider child in the process.

A note on what is deliberately absent: there is no authenticated-proxy support. Phase 0F measured
that an env-injected proxy credential is not a security boundary — the provider child sends
``Proxy-Authorization`` itself, an agent-executed grandchild inherits the value, and a same-user
process can open the runtime process with ``PROCESS_VM_READ``. A credential in the URL is
therefore rejected at parse time rather than warned about (§7.3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The dedicated Agent proxy namespace. These names are frozen by Phase 0F and are never handed to
#: a provider: they exist only to be mapped downward (§7.2 rule 4).
AGENT_PROXY_URL_ENV = "SERVERFS_AGENT_PROXY_URL"
AGENT_NO_PROXY_ENV = "SERVERFS_AGENT_NO_PROXY"

#: Mandatory local bypass (§7.2). Phase 0F measured that an absent or empty NO_PROXY re-enables
#: proxying of loopback, which would route the Bridge's own authenticated Codex control channel
#: through the egress proxy. The operator value merges with this set and can never remove an entry.
MANDATORY_NO_PROXY: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

#: Every proxy variable spelling that must never survive into a child environment, in either case.
#: Phase 0F measured both cases work for Codex, which is exactly why leaving the lower-case form
#: behind would be a silent egress leak rather than a harmless extra.
PROXY_ENV_NAMES: frozenset[str] = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})

#: Suffix marking a name as an ambient proxy endpoint rather than a proxy setting.
#:
#: Measured on WorkPC during Phase E: a vendor tool exported ``*_PROXY_URL`` and the four standard
#: names alone did not match it, so it reached the Bridge process and would have been forwarded
#: on to a provider child. §7.1 requires the provider proxy environment to be decided by policy, so
#: an unrelated ambient endpoint must not survive the scrub.
#:
#: The match is a suffix and not ``"proxy" in name.lower()``. A substring rule would also swallow
#: names that merely mention proxying (``PROXY_PROTOCOL_VERSION``, ``PROXY_MODE``) and so remove
#: provider configuration nobody measured. This captures the shape actually seen plus its obvious
#: variants, and invents nothing further. The Bridge package carries the same rule independently, as
#: §23/§70 require; tests pin the two to the same behaviour rather than sharing an import.
PROXY_URL_SUFFIX = "_PROXY_URL"

#: Namespaces that must never reach the Bridge process, because both provider SDKs copy its whole
#: environment into the CLI child and from there into agent-executed tool code (Phase 0F §7).
#:
#: The Agent entries are the *namespace* prefixes, not the two concrete variable names. Naming the
#: variables would leave a sibling such as ``SERVERFS_AGENT_NO_PROXY`` outside the scrub, which is
#: the kind of gap that only shows up as a leak: the endpoint would be removed and its bypass list
#: would survive into a process that has no endpoint to apply it to.
SCRUBBED_ENV_PREFIXES: tuple[str, ...] = (
    "CONTROL_PLANE_",
    "TUNNEL_CLIENT_",
    "MCP_",
    "OPENAI_",
    "SERVERFS_PROXY_",
    "SERVERFS_AGENT_",
)

#: The subset that must additionally be absent from a *provider child*. The full scrubbed set is
#: already applied by `bridge_environment` before the Bridge starts, so this list exists for defence
#: in depth: a child builder handed an unsanitized base must still not forward a credential.
CHILD_SCRUBBED_PREFIXES: tuple[str, ...] = (
    "SERVERFS_PROXY_",
    "SERVERFS_AGENT_",
)

#: Accepted schemes. SOCKS5 is out of scope by §7.1, and an https:// endpoint is accepted because a
#: TLS-terminating forwarder is a legitimate deployment; it is still a credentialless endpoint.
ALLOWED_PROXY_SCHEMES = frozenset({"http", "https"})


class AgentProxyError(Exception):
    """The dedicated Agent proxy configuration is unusable; refuse startup."""


@dataclass(frozen=True)
class AgentProxyConfig:
    """A validated, credentialless Agent egress endpoint held only in memory.

    ``repr=False`` on ``url`` is defence in depth for the most likely accident in this codebase:
    an exception message or a log line that formats a config object. The value still must never be
    logged deliberately, but it should not be one ``repr()`` away from a transcript either.
    """

    url: str
    no_proxy: str
    source: str = "env"

    def __repr__(self) -> str:
        return f"AgentProxyConfig(source={self.source!r}, redacted=True)"


#: Lower-case spelling of the proxy names, precomputed because the case-fold check runs per
#: variable and the same four names are tested on every call.
_PROXY_NAMES_LOWER = {name.lower() for name in PROXY_ENV_NAMES}


def _is_proxy_variable(name: str) -> bool:
    """Whether a name configures proxying for this or any other consumer.

    Two shapes, both measured: the four standard variables in either case, and an ambient
    ``*_PROXY_URL`` endpoint exported by something other than ServerFS. The second exists because a
    name outside this list would reach the Bridge process and then a provider child, which is
    exactly the inheritance §7.1 forbids.
    """
    upper = name.upper()
    if upper in PROXY_ENV_NAMES or name.lower() in _PROXY_NAMES_LOWER:
        return True
    return upper.endswith(PROXY_URL_SUFFIX) or name.lower().endswith(PROXY_URL_SUFFIX.lower())


def _has_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.upper().startswith(prefix) for prefix in prefixes)


def _scrubbed(name: str) -> bool:
    """Whether one environment variable must not survive into a Bridge environment."""
    return _is_proxy_variable(name) or _has_prefix(name, SCRUBBED_ENV_PREFIXES)


def bridge_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment the Bridge process may run with (§7.2 rule 3).

    Phase 0F measured that both provider SDKs inherit the Bridge environment wholesale into the
    provider CLI child, and that Claude's SDK offers no way to unset an inherited name. This is
    therefore the only place where Tunnel/Control Plane credentials and proxy variables can be kept
    out of provider children: they must never enter the Bridge process in the first place.

    Provider-native environment a runtime legitimately needs — ``PATH``, ``HOME``,
    ``USERPROFILE``, ``LOCALAPPDATA``, ``CODEX_HOME`` and the rest — is deliberately preserved.
    Only the proxy trust domain and the credential-bearing namespaces are removed.

    The Agent proxy *endpoint* is also absent here on purpose. It travels to the Bridge over the
    private bootstrap channel (D4) and is mapped downward per child, so it never sits in an
    environment that is copied wholesale.
    """
    env = dict(os.environ if source is None else source)
    return {name: value for name, value in env.items() if not _scrubbed(name)}


def merge_no_proxy(operator_value: str | None) -> str:
    """The effective child ``NO_PROXY``: the operator value merged with the mandatory set.

    The operator can add entries but can never remove a mandatory one, and an absent or empty
    operator value must not disable the bypass — both measured in Phase 0F. Output is trimmed,
    empty-free, de-duplicated and order-stable so the same input always yields the same string.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in (operator_value or "").replace(";", ",").split(","):
        candidate = raw.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        entries.append(candidate)
    for mandatory in MANDATORY_NO_PROXY:
        if mandatory not in seen:
            seen.add(mandatory)
            entries.append(mandatory)
    return ",".join(entries)


def parse_agent_proxy(settings, env: Mapping[str, str] | None = None) -> AgentProxyConfig | None:
    """Build the runtime proxy config from the dedicated namespace, or ``None`` when disabled.

    Consumption happens only when ``[agent.proxy]`` is enabled *and* sourced from the environment.
    A disabled proxy means the ambient ``SERVERFS_AGENT_PROXY_URL`` is deliberately **not** read,
    so a provider can never silently inherit an endpoint nobody opted into (§7.1).

    A malformed or credential-bearing endpoint fails closed rather than degrading to direct: an
    operator who asked for a proxy and got none would see provider traffic leave the host without
    any indication.
    """
    if settings is None or not settings.enabled:
        return None
    if settings.source != "env":  # defensive: the config parser admits only "env" today
        raise AgentProxyError("only the env proxy source is supported")

    environ = os.environ if env is None else env
    raw_url = environ.get(AGENT_PROXY_URL_ENV, "").strip()
    if not raw_url:
        raise AgentProxyError(f"{AGENT_PROXY_URL_ENV} is required when [agent.proxy] is enabled")
    url = _validated_endpoint(raw_url)
    return AgentProxyConfig(
        url=url,
        no_proxy=merge_no_proxy(environ.get(AGENT_NO_PROXY_ENV)),
        source=settings.source,
    )


def _validated_endpoint(raw: str) -> str:
    """Validate one absolute credentialless http(s) endpoint with an explicit port."""
    if "\x00" in raw or any(char.isspace() for char in raw):
        # Whitespace in a URL is ambiguous across parsers; refuse rather than normalize it away.
        raise AgentProxyError("the Agent proxy URL contains an invalid character")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_PROXY_SCHEMES:
        # Covers socks5:// and any other scheme, which are out of scope by §7.1.
        raise AgentProxyError(
            f"the Agent proxy URL scheme must be one of {sorted(ALLOWED_PROXY_SCHEMES)}"
        )
    if not parts.hostname:
        raise AgentProxyError("the Agent proxy URL must include a host")
    if parts.port is None:
        # Phase 0F froze an explicit port: a default-port proxy would make the endpoint ambiguous.
        raise AgentProxyError("the Agent proxy URL must include an explicit port")
    if parts.username or parts.password or "@" in parts.netloc:
        # Phase 0F §5 measured that a userinfo credential is not a boundary: the child forwards it,
        # a grandchild inherits it, and a same-user process can read it. §7.3 forbids the shape.
        raise AgentProxyError("the Agent proxy URL must not contain credentials")
    if parts.fragment:
        raise AgentProxyError("the Agent proxy URL must not contain a fragment")
    return raw


def build_runtime_environment(
    base_env: Mapping[str, str],
    *,
    runtime: str,
    use_proxy: bool,
    proxy: AgentProxyConfig | None = None,
) -> dict[str, str]:
    """The environment for one runtime's network-owning child (§7.2, Phase 0F §7).

    This is a downward mapping, never an overlay on a wholesale copy. The proxy trust domain is
    cleared first and then re-established from explicit policy, so what a child sees is decided by
    ``use_proxy`` and not by whatever the parent happened to carry:

    - ``use_proxy=false`` yields a **proxy-free** child even when the parent has proxy variables
      and even when an endpoint is configured. That is the property Phase 0F §7.2 rule 2 requires,
      because a ``use_proxy=false`` runtime cannot be cleaned after the fact under Claude.
    - ``use_proxy=true`` injects only the canonical ``HTTPS_PROXY`` + ``NO_PROXY`` pair. Phase 0F
      measured ``HTTP_PROXY`` alone insufficient for HTTPS destinations, and injecting more than
      the minimum would only widen the surface.
    - ``SERVERFS_AGENT_PROXY_*`` and ``SERVERFS_PROXY_*`` never reach the child under any policy:
      providers do not consume those names, so forwarding them would be pure exposure.
    """
    env = {
        name: value
        for name, value in base_env.items()
        if not _is_proxy_variable(name) and not _has_prefix(name, CHILD_SCRUBBED_PREFIXES)
    }
    if use_proxy:
        if proxy is None:
            # Policy says route through the Agent proxy but nothing configured one. Fail loudly
            # rather than silently sending provider traffic direct.
            raise AgentProxyError(
                f"runtime {runtime!r} has use_proxy=true but no Agent proxy endpoint is configured"
            )
        env["HTTPS_PROXY"] = proxy.url
        env["NO_PROXY"] = proxy.no_proxy
    return env


__all__ = [
    "AGENT_NO_PROXY_ENV",
    "AGENT_PROXY_URL_ENV",
    "ALLOWED_PROXY_SCHEMES",
    "MANDATORY_NO_PROXY",
    "PROXY_ENV_NAMES",
    "SCRUBBED_ENV_PREFIXES",
    "AgentProxyConfig",
    "AgentProxyError",
    "bridge_environment",
    "build_runtime_environment",
    "merge_no_proxy",
    "parse_agent_proxy",
]
