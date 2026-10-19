"""DingTalk OAuth adapter contract tests."""

from __future__ import annotations

import json
import urllib.parse
from collections import deque

import pytest

from serverfs_mcp.oauth_providers.dingtalk import DingTalkOAuthError, DingTalkOAuthProvider

CALLBACK = "https://auth.example.com/oauth/provider/dingtalk/callback"


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


class TestDingTalkAuthorizationRequest:
    def test_authorization_url_uses_openid_scope_state_and_consent(self) -> None:
        provider = DingTalkOAuthProvider("client-id", "client-secret")
        url = provider.authorization_url(callback_url=CALLBACK, state="state-123")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://login.dingtalk.com/oauth2/auth"
        )
        assert query == {
            "redirect_uri": [CALLBACK],
            "response_type": ["code"],
            "client_id": ["client-id"],
            "scope": ["openid"],
            "state": ["state-123"],
            "prompt": ["consent"],
        }


class TestDingTalkIdentityExchange:
    def test_code_exchange_and_user_lookup_return_union_id_principal(self) -> None:
        provider = DingTalkOAuthProvider("client-id", "client-secret", timeout_seconds=1.25)
        opener = FakeOpener(
            FakeResponse(
                {
                    "accessToken": "dingtalk-provider-token",
                    "refreshToken": "must-not-be-persisted",
                    "expireIn": 7200,
                }
            ),
            FakeResponse(
                {
                    "nick": "Mutable Nick",
                    "openId": "app-open-id",
                    "unionId": "stable-union-id",
                }
            ),
        )
        object.__setattr__(provider, "_opener", lambda: opener)

        principal = provider._authenticate_sync(auth_code="temporary-auth-code")
        assert principal.canonical == "dingtalk:stable-union-id"
        assert len(opener.requests) == 2
        assert opener.timeouts == [1.25, 1.25]

        token_request, user_request = opener.requests
        assert token_request.full_url == "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
        assert token_request.get_method() == "POST"
        assert json.loads(token_request.data.decode("utf-8")) == {
            "clientId": "client-id",
            "clientSecret": "client-secret",
            "code": "temporary-auth-code",
            "grantType": "authorization_code",
        }
        assert token_request.get_header("Content-type") == "application/json"

        assert user_request.full_url == "https://api.dingtalk.com/v1.0/contact/users/me"
        assert user_request.get_method() == "GET"
        assert user_request.get_header("X-acs-dingtalk-access-token") == "dingtalk-provider-token"

    @pytest.mark.parametrize("token_payload", [{}, {"accessToken": ""}])
    def test_invalid_token_response_fails_without_exposing_provider_material(
        self, token_payload: dict[str, object]
    ) -> None:
        provider = DingTalkOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(FakeResponse(token_payload))
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(DingTalkOAuthError) as error:
            provider._authenticate_sync(auth_code="temporary-auth-code")
        rendered = str(error.value)
        assert "client-secret" not in rendered
        assert "temporary-auth-code" not in rendered
        assert "dingtalk-provider-token" not in rendered

    @pytest.mark.parametrize("union_id", [None, "", "   "])
    def test_user_identity_requires_union_id(self, union_id: object) -> None:
        provider = DingTalkOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(
            FakeResponse({"accessToken": "token", "expireIn": 7200}),
            FakeResponse({"unionId": union_id, "openId": "app-open-id"}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(DingTalkOAuthError, match="stable unionId"):
            provider._authenticate_sync(auth_code="temporary-auth-code")
