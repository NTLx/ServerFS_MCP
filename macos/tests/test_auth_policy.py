"""Admission-policy tests for upstream OAuth identities."""

from __future__ import annotations

import pytest

from serverfs_mcp.auth_policy import AdmissionPolicy, parse_principal, principal_from_parts


class TestPrincipal:
    def test_canonical_identity_and_stable_audit_id(self) -> None:
        principal = principal_from_parts("GitHub", "123456")
        assert principal.provider == "github"
        assert principal.subject == "123456"
        assert principal.canonical == "github:123456"
        assert len(principal.audit_id) == 16
        assert principal.audit_id == parse_principal("github:123456").audit_id
        assert "123456" not in principal.audit_id

    @pytest.mark.parametrize(
        ("provider", "subject"),
        [
            ("unknown", "123"),
            ("github", ""),
            ("github", "user name"),
            ("github", "x" * 513),
        ],
    )
    def test_invalid_principal_fails_closed(self, provider: str, subject: str) -> None:
        with pytest.raises(ValueError):
            principal_from_parts(provider, subject)

    def test_parse_requires_provider_separator(self) -> None:
        with pytest.raises(ValueError):
            parse_principal("github123")


class TestAdmissionPolicy:
    def test_default_empty_policy_denies(self) -> None:
        policy = AdmissionPolicy(())
        assert policy.allows(parse_principal("github:123")) is False

    def test_exact_provider_subject_match_only(self) -> None:
        policy = AdmissionPolicy(("github:123", "feishu:ou_abc"))
        assert policy.allows(parse_principal("github:123")) is True
        assert policy.allows(parse_principal("github:124")) is False
        assert policy.allows(parse_principal("feishu:ou_abc")) is True
        assert policy.allows(parse_principal("dingtalk:ou_abc")) is False
