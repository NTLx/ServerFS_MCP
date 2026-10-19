"""Feishu OAuth adapter contract tests."""

from __future__ import annotations

import json
import urllib.parse
from collections import deque

import pytest

from serverfs_mcp.oauth_providers.feishu import FeishuOAuthError, FeishuOAuthProvider

CALLBACK = "https://auth.example.com/oauth/provider/feishu/callback"


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


class TestFeishuAuthorizationRequest:
    def test_authorization_url_uses_minimal_identity_scope_and_state(self) -> None:
        provider = FeishuOAuthProvider("client-id", "client-secret")
        url = provider.authorization_url(callback_url=CALLBACK, state="state-123")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
        )
        assert query == {
            "client_id": ["client-id"],
            "response_type": ["code"],
            "redirect_uri": [CALLBACK],
            "scope": ["contact:user.base:readonly"],
            "state": ["state-123"],
        }
        assert "offline_access" not in query["scope"][0]


class TestFeishuIdentityExchange:
    def test_code_exchange_and_user_lookup_return_open_id_principal(self) -> None:
        provider = FeishuOAuthProvider("client-id", "client-secret", timeout_seconds=1.25)
        opener = FakeOpener(
            FakeResponse(
                {
                    "code": 0,
                    "access_token": "feishu-provider-token",
                    "token_type": "Bearer",
                    "expires_in": 7200,
                }
            ),
            FakeResponse(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "open_id": "ou_stable123",
                        "name": "Mutable Name",
                    },
                }
            ),
        )
        object.__setattr__(provider, "_opener", lambda: opener)

        principal = provider._authenticate_sync(code="temporary-code", callback_url=CALLBACK)
        assert principal.canonical == "feishu:ou_stable123"
        assert len(opener.requests) == 2
        assert opener.timeouts == [1.25, 1.25]

        token_request, user_request = opener.requests
        assert token_request.full_url == "https://accounts.feishu.cn/oauth/v3/token"
        assert token_request.get_method() == "POST"
        token_form = urllib.parse.parse_qs(token_request.data.decode("utf-8"))
        assert token_form == {
            "grant_type": ["authorization_code"],
            "client_id": ["client-id"],
            "client_secret": ["client-secret"],
            "code": ["temporary-code"],
            "redirect_uri": [CALLBACK],
        }
        assert token_request.get_header("Content-type") == "application/x-www-form-urlencoded"

        assert user_request.full_url == "https://open.feishu.cn/open-apis/authen/v1/user_info"
        assert user_request.get_method() == "GET"
        assert user_request.get_header("Authorization") == "Bearer feishu-provider-token"

    @pytest.mark.parametrize(
        "token_payload",
        [
            {"code": 20003, "msg": "invalid code"},
            {"code": 0, "token_type": "Bearer"},
            {"code": 0, "access_token": "token", "token_type": "MAC"},
        ],
    )
    def test_invalid_token_response_fails_without_exposing_provider_material(
        self, token_payload: dict[str, object]
    ) -> None:
        provider = FeishuOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(FakeResponse(token_payload))
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(FeishuOAuthError) as error:
            provider._authenticate_sync(code="temporary-code", callback_url=CALLBACK)
        rendered = str(error.value)
        assert "client-secret" not in rendered
        assert "temporary-code" not in rendered
        assert "feishu-provider-token" not in rendered

    @pytest.mark.parametrize("open_id", [None, "", "user-123", "ou_"])
    def test_user_identity_requires_app_scoped_open_id(self, open_id: object) -> None:
        provider = FeishuOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(
            FakeResponse({"code": 0, "access_token": "token", "token_type": "Bearer"}),
            FakeResponse({"code": 0, "data": {"open_id": open_id}}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(FeishuOAuthError, match="stable open_id"):
            provider._authenticate_sync(code="temporary-code", callback_url=CALLBACK)

    def test_user_lookup_error_fails_closed(self) -> None:
        provider = FeishuOAuthProvider("client-id", "client-secret")
        opener = FakeOpener(
            FakeResponse({"code": 0, "access_token": "token", "token_type": "Bearer"}),
            FakeResponse({"code": 99991668, "msg": "invalid user token"}),
        )
        object.__setattr__(provider, "_opener", lambda: opener)
        with pytest.raises(FeishuOAuthError, match="user lookup failed"):
            provider._authenticate_sync(code="temporary-code", callback_url=CALLBACK)
