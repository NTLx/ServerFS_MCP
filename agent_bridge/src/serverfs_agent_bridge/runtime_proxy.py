"""Bridge-owned runtime egress policy for provider children (v0.11 Phase D, §7.2, §15 D2).

Phase 0F measured that both provider SDKs build their CLI child environment by copying the **whole**
Bridge environment, and that Claude's SDK cannot unset an inherited name. That measurement produces
one rule, and this module is where it is applied downward:

    the effective environment of a runtime's network-owning child is decided by policy, never
    inherited.

The endpoint arrives over the private bootstrap channel and lives in this process's memory only. It
is never written to ``bridge.json``, never placed in ``os.environ``, never passed in argv, and never
logged. From here it exists in exactly one more place: the child that is supposed to use it.

Why this lives in the Bridge package rather than importing the serverfs one: §23/§70 freeze the two
packages as independent, and this code runs on the provider-spawn path where a cross-package import
would be both forbidden and a layering inversion. The policy is reimplemented narrowly here, and the
two implementations are pinned to the same behaviour by tests rather than by a shared import.

The mapping is the Phase 0F measurement, not a preference: ``HTTPS_PROXY`` alone is sufficient for
HTTPS destinations while ``HTTP_PROXY`` alone is not, so exactly that pair is injected and nothing
else. Injecting ``HTTP_PROXY`` or ``ALL_PROXY`` as well would only widen the surface.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .bootstrap import RuntimeProxy

#: Every spelling of every standard proxy variable. Phase 0F measured that the installed Codex build
#: accepts both cases, which is precisely why leaving the lower-case form behind would be a silent
#: egress change rather than a harmless extra.
PROXY_VARIABLE_NAMES: frozenset[str] = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
)
_PROXY_NAMES_LOWER = {name.lower() for name in PROXY_VARIABLE_NAMES}

#: Suffix marking a name as an ambient proxy endpoint rather than a proxy setting.
#:
#: Measured on WorkPC during Phase E: a vendor tool exported ``*_PROXY_URL`` and the four standard
#: names alone did not match it, so it was forwarded into the provider child verbatim. That breaks
#: the frozen rule above -- the child's egress is decided by policy, not by whatever the host
#: happens to export -- and it is a trust-boundary defect, not a cosmetic one.
#:
#: The match is deliberately a suffix and not ``"proxy" in name.lower()``. A substring rule would
#: also swallow names that merely mention proxying (``PROXY_PROTOCOL_VERSION``, ``PROXY_MODE``) and
#: so silently remove provider configuration this phase never measured. The suffix captures the
#: shape actually observed plus its obvious variants, and nothing is invented beyond that.
PROXY_URL_SUFFIX = "_PROXY_URL"
_PROXY_URL_SUFFIX_LOWER = PROXY_URL_SUFFIX.lower()

#: Namespaces that must never be forwarded to a provider child. ``SERVERFS_AGENT_*`` is here because
#: those names exist only to be mapped downward — providers do not consume them, so forwarding them
#: would be pure exposure. ``SERVERFS_PROXY_*`` is the Tunnel trust domain, which is a *different*
#: domain entirely and must never be bulk-forwarded into Agent egress (§7.1).
FORWARD_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "SERVERFS_AGENT_",
    "SERVERFS_PROXY_",
    "CONTROL_PLANE_",
    "TUNNEL_CLIENT_",
    "MCP_",
)

#: Provider-native names that must survive: removing these would break provider auth and config.
#: Phase 0F measured that provider-native credential/config *files* stay available; this list is
#: the environment half of the same requirement.
PRESERVED_SAMPLE: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "CODEX_HOME",
)


def _is_proxy_variable(name: str) -> bool:
    """Whether a name configures proxying for this or any other consumer.

    Two shapes, both measured: the four standard variables in either case, and an ambient
    ``*_PROXY_URL`` endpoint exported by something other than ServerFS. The second exists because a
    name outside this list would otherwise be forwarded to the provider child and decide its egress.
    """
    upper = name.upper()
    if upper in PROXY_VARIABLE_NAMES or name.lower() in _PROXY_NAMES_LOWER:
        return True
    return upper.endswith(PROXY_URL_SUFFIX) or name.lower().endswith(_PROXY_URL_SUFFIX_LOWER)


def _is_forbidden_prefix(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in FORWARD_FORBIDDEN_PREFIXES)


class RuntimeProxyError(Exception):
    """The runtime proxy policy cannot be satisfied; the caller must fail closed.

    Redacted by construction: the message names the failure class and never the endpoint.
    """


def build_runtime_environment(
    base_env: Mapping[str, str],
    *,
    runtime: str,
    use_proxy: bool,
    proxy: RuntimeProxy | None = None,
) -> dict[str, str]:
    """The environment for one runtime's network-owning child.

    This is a downward mapping, not an overlay. The proxy trust domain is cleared first and then
    re-established from explicit policy, so what the child sees is decided by ``use_proxy`` rather
    than by whatever the parent happened to be carrying:

    - ``use_proxy=false`` yields a genuinely **proxy-free** child even when the parent has proxy
      variables and even when an endpoint is configured. This is the property Phase 0F requires,
      because such a child cannot be cleaned afterwards under Claude.
    - ``use_proxy=true`` injects only ``HTTPS_PROXY`` and ``NO_PROXY``. The mandatory bypass is
      already merged into ``proxy.no_proxy`` by the supervisor; it is re-asserted here so the
      invariant holds even if a caller constructs a ``RuntimeProxy`` by hand.
    """
    env = {
        name: value
        for name, value in base_env.items()
        if not _is_proxy_variable(name) and not _is_forbidden_prefix(name)
    }
    if not use_proxy:
        return env
    if proxy is None:
        # Policy says route through the Agent proxy but nothing configured one. Failing loudly beats
        # silently sending provider traffic direct.
        raise RuntimeProxyError(
            f"runtime {runtime!r} has use_proxy=true but no Agent proxy endpoint is configured"
        )
    env["HTTPS_PROXY"] = proxy.url
    env["NO_PROXY"] = _with_mandatory_bypass(proxy.no_proxy)
    return env


def _with_mandatory_bypass(no_proxy: str) -> str:
    """Re-assert the mandatory loopback set on whatever the supervisor merged.

    Defence in depth for the §7.2 rule: the supervisor already merges these, but the invariant that
    a provider child can never proxy its own loopback control channel belongs here too, where the
    child environment is actually built. Phase 0F measured that an absent or empty ``NO_PROXY``
    re-enables proxying of loopback.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in (no_proxy or "").replace(";", ",").split(","):
        candidate = raw.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            entries.append(candidate)
    for mandatory in ("127.0.0.1", "localhost", "::1"):
        if mandatory not in seen:
            seen.add(mandatory)
            entries.append(mandatory)
    return ",".join(entries)


def process_environment_is_clean(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process's own environment carries no proxy variable and no Agent namespace.

    A startup assertion rather than a hope: Phase 0F's rule is that the *Bridge* environment must be
    clean, because that is what the SDKs copy wholesale. Checking it here means a deployment that
    somehow acquired a proxy variable is caught at the point the policy is applied.
    """
    environ = os.environ if env is None else env
    return not [
        name
        for name in environ
        if _is_proxy_variable(name)
        or name.upper().startswith("SERVERFS_AGENT_")
        or name.upper().startswith("SERVERFS_PROXY_")
    ]


def build_runtime_environment_overlay(
    base_env: Mapping[str, str],
    *,
    runtime: str,
) -> dict[str, str | None]:
    """The same scrub, expressed for an SDK that inherits its parent's environment.

    The Qoder SDK builds its child's environment as ``{**os.environ}`` plus an overlay in which a
    value of ``None`` **deletes** the variable. That is the opposite shape from
    ``build_runtime_environment``, which returns a complete environment for ``Popen(env=...)``.
    Handing it the full environment would not work twice over: the SDK would ignore the intent, and
    the scrub rule would be restated in a second place where the Phase E ``*_PROXY_URL`` fix would
    immediately start drifting.

    So this is a **diff** against that function's own result, sent as deletions only. The policy has
    exactly one implementation; the SDK simply learns the difference. When the rule changes, a Qoder
    child inherits the change rather than needing a second edit.

    The diff also keeps the provider's own configuration intact. Only names the scrub would remove
    appear, so ``PATH``, ``USERPROFILE`` and provider-native settings are left to the SDK's normal
    inheritance, and nothing is denied that the policy never intended to touch.
    """
    target = build_runtime_environment(base_env, runtime=runtime, use_proxy=False)
    keys = base_env.keys() | target.keys()
    return {
        key: (target[key] if key in target else None)
        for key in keys
        if base_env.get(key) != target.get(key)
    }


__all__ = [
    "FORWARD_FORBIDDEN_PREFIXES",
    "PRESERVED_SAMPLE",
    "PROXY_VARIABLE_NAMES",
    "RuntimeProxyError",
    "build_runtime_environment",
    "build_runtime_environment_overlay",
    "process_environment_is_clean",
]
