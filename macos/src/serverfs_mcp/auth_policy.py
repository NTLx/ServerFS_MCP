"""Identity admission policy for the optional ServerFS OAuth broker."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

_SUPPORTED_PROVIDERS = frozenset({"github", "dingtalk", "feishu", "wechat"})
_MAX_SUBJECT_BYTES = 512


@dataclass(frozen=True)
class Principal:
    """Stable upstream identity used only for ServerFS instance admission."""

    provider: str
    subject: str

    @property
    def canonical(self) -> str:
        return f"{self.provider}:{self.subject}"

    @property
    def audit_id(self) -> str:
        """Short stable non-reversible identifier suitable for routine logs."""

        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()[:16]


def principal_from_parts(provider: str, subject: str) -> Principal:
    provider = provider.strip().lower()
    subject = subject.strip()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported OAuth identity provider {provider!r}")
    if not subject or any(char.isspace() for char in subject):
        raise ValueError("OAuth identity subject must be non-empty and contain no whitespace")
    if len(subject.encode("utf-8")) > _MAX_SUBJECT_BYTES:
        raise ValueError("OAuth identity subject is too large")
    return Principal(provider=provider, subject=subject)


def parse_principal(value: str) -> Principal:
    provider, separator, subject = value.partition(":")
    if not separator:
        raise ValueError("OAuth principal must be provider:subject")
    return principal_from_parts(provider, subject)


class AdmissionPolicy:
    """Exact-match default-deny admission allowlist."""

    def __init__(self, allowed_principals: tuple[str, ...]) -> None:
        principals = tuple(parse_principal(value) for value in allowed_principals)
        self._allowed = frozenset(principal.canonical for principal in principals)

    def allows(self, principal: Principal) -> bool:
        return principal.canonical in self._allowed
