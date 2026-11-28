"""WeChat Open Platform website-login adapter contract tests."""

from __future__ import annotations

import json
import urllib.parse
from collections import deque

import pytest

from serverfs_mcp.oauth_providers.wechat import WeChatOAuthError, WeChatOAuthProvider

CALLBACK = "https://auth.example.com/oauth/provider/wechat/callback"


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


class TestWeChatAuthorizationRequest:
    def test_authorization_url_uses_website_scope_state_and_fragment(self) -> None:
        provider = WeChatOAuthProvider("wx-app-id", "app-secret")
        url = provider.authorization_url(callback_url=CALLBACK, state="state-123")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://open.weixin.qq.com/connect/qrconnect"
        )
        assert query == {
            "appid": ["wx-app-id"],
            "redirect_uri": [CALLBACK],
            "response_type": ["code"],
            "scope": ["snsapi_login"],
            "state": ["state-123"],
        }
        assert parsed.fragment == "wechat_redirect"


class TestWeChatIdentityExchange:
    def test_code_exchange_and_user_lookup_return_union_id_principal(self) -> None:
        provider = WeChatOAuthProvider("wx-app-id", "app-secret", timeout_seconds=1.25)
        opener = FakeOpener(
            FakeResponse(
                {
                    "access_token": "wechat-provider-token",
                    "expires_in": 7200,
                    "refresh_token": "must-not-be-persisted",
                    "openid": "openid-123",
                    "scope": "snsapi_login",
                }
            ),
            FakeResponse(
                {
                    "openid": "openid-123",
                    "nickname": "Mutable Nickname",
                    "unionid": "stable-union-id",
                }
            ),
        )
        object.__setattr__(provider, "_opener", lambda: opener)

        principal = provider._authenticate_sync(code="temporary-code")
        assert principal.canonical == "wechat:stable-union-id"
        assert len(opener.requests) == 2
        assert opener.timeouts == [1.25, 1.25]

        token_request, user_request = opener.requests
        token_url = urllib.parse.urlparse(token_request.full_url)
        assert f"{token_url.scheme}://{token_url.netloc}{token_url.path}" == (
            "https://api.weixin.qq.com/sns/oauth2/access_token"
        )
        assert urllib.parse.parse_qs(token_url.query) == {
            "appid": ["wx-app-id"],
            "secret": ["app-secret"],
            "code": ["temporary-code"],
            "grant_type": ["authorization_code"],
        }
        assert token_request.get_method() == "GET"

        user_url = urllib.parse.urlparse(user_request.full_url)
        assert f"{user_url.scheme}://{user_url.netloc}{user_url.path}" == (
            "https://api.weixin.qq.com/sns/userinfo"
        )
        assert urllib.parse.parse_qs(user_url.query) == {
            "access_token": ["wechat-provider-token"],
            "openid": ["openid-123"],
        }
        assert user_request.get_method() == "GET"

    @pytest.mark.parametrize(
        "token_payload",
        [
            {"errcode": 40029, "errmsg": "invalid code"},
            {"openid": "openid-123"},
            {"access_token": "token"},
        ],
    )
    def test_invalid_token_response_fails_without_exposing_provider_material(
        self, token_payload: dict[str, object]
    ) -> None:
        provider = WeChatOAuthProvider("wx-app-id", "app-secret")
        opener = FakeOpener(FakeResponse(token_payload))
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(WeChatOAuthError) as error:
            provider._authenticate_sync(code="temporary-code")
        rendered = str(error.value)
        assert "app-secret" not in rendered
        assert "temporary-code" not in rendered
        assert "wechat-provider-token" not in rendered

    @pytest.mark.parametrize("union_id", [None, "", "   "])
    def test_user_identity_requires_union_id(self, union_id: object) -> None:
        provider = WeChatOAuthProvider("wx-app-id", "app-secret")
        opener = FakeOpener(
            FakeResponse({"access_token": "token", "openid": "openid-123"}),
            FakeResponse({"openid": "openid-123", "unionid": union_id}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(WeChatOAuthError, match="stable unionid"):
            provider._authenticate_sync(code="temporary-code")

    def test_user_openid_must_match_token_subject(self) -> None:
        provider = WeChatOAuthProvider("wx-app-id", "app-secret")
        opener = FakeOpener(
            FakeResponse({"access_token": "token", "openid": "openid-123"}),
            FakeResponse({"openid": "other-openid", "unionid": "stable-union-id"}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(WeChatOAuthError, match="did not match"):
            provider._authenticate_sync(code="temporary-code")
