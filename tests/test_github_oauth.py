"""GitHub OAuth App adapter contract tests."""

from __future__ import annotations

import json
import urllib.parse
from collections import deque

import pytest

from serverfs_mcp.oauth_providers.github import GitHubOAuthError, GitHubOAuthProvider

CALLBACK = "https://auth.example.com/oauth/provider/github/callback"


class FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            return self.body
        return self.body[:limit]


class FakeOpener:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = deque(responses)
        self.requests = []
        self.timeouts: list[float] = []

    def open(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.responses.popleft()


class TestGitHubAuthorizationRequest:
    def test_authorization_url_uses_state_pkce_and_no_scope(self) -> None:
        provider = GitHubOAuthProvider("client-id", "client-secret")
        url = provider.authorization_url(
            callback_url=CALLBACK,
            state="state-123",
            code_challenge="challenge-123",
        )
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://github.com/login/oauth/authorize"
        )
        assert query == {
            "client_id": ["client-id"],
            "redirect_uri": [CALLBACK],
            "state": ["state-123"],
            "code_challenge": ["challenge-123"],
            "code_challenge_method": ["S256"],
        }
        assert "scope" not in query


class TestGitHubIdentityExchange:
    def test_code_exchange_and_user_lookup_return_numeric_principal(self) -> None:
        provider = GitHubOAuthProvider("client-id", "client-secret", timeout_seconds=1.25)
        opener = FakeOpener(
            FakeResponse({"access_token": "github-provider-token", "token_type": "bearer"}),
            FakeResponse({"id": 123456, "login": "mutable-login"}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)

        principal = provider._authenticate_sync(
            code="temporary-code",
            callback_url=CALLBACK,
            code_verifier="pkce-verifier",
        )
        assert principal.canonical == "github:123456"
        assert len(opener.requests) == 2
        assert opener.timeouts == [1.25, 1.25]

        token_request, user_request = opener.requests
        assert token_request.full_url == "https://github.com/login/oauth/access_token"
        assert token_request.get_method() == "POST"
        token_form = urllib.parse.parse_qs(token_request.data.decode("utf-8"))
        assert token_form == {
            "client_id": ["client-id"],
            "client_secret": ["client-secret"],
            "code": ["temporary-code"],
            "redirect_uri": [CALLBACK],
            "code_verifier": ["pkce-verifier"],
        }
        assert token_request.get_header("Accept") == "application/json"

        assert user_request.full_url == "https://api.github.com/user"
        assert user_request.get_method() == "GET"
        assert user_request.get_header("Authorization") == "Bearer github-provider-token"
        assert user_request.get_header("Accept") == "application/vnd.github+json"

    @pytest.mark.parametrize(
        "token_payload",
        [
            {"error": "bad_verification_code"},
            {"token_type": "bearer"},
            {"access_token": "token", "token_type": "mac"},
        ],
    )
    def test_invalid_token_response_fails_without_exposing_provider_material(
        self, token_payload: dict[str, object]
    ) -> None:
        provider = GitHubOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(FakeResponse(token_payload))
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(GitHubOAuthError) as error:
            provider._authenticate_sync(
                code="temporary-code",
                callback_url=CALLBACK,
                code_verifier="pkce-verifier",
            )
        rendered = str(error.value)
        assert "client-secret" not in rendered
        assert "temporary-code" not in rendered
        assert "github-provider-token" not in rendered

    @pytest.mark.parametrize("user_id", [None, "123", 0, -1, True])
    def test_user_identity_requires_positive_numeric_id(self, user_id: object) -> None:
        provider = GitHubOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(
            FakeResponse({"access_token": "provider-token", "token_type": "bearer"}),
            FakeResponse({"id": user_id, "login": "name"}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(GitHubOAuthError, match="stable numeric id"):
            provider._authenticate_sync(
                code="temporary-code",
                callback_url=CALLBACK,
                code_verifier="pkce-verifier",
            )
