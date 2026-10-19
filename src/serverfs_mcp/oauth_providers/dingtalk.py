"""DingTalk OAuth identity adapter for ``serverfs-auth``."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..auth_policy import Principal, principal_from_parts
from ..shared_proxy import SharedProxyConfig

_DINGTALK_AUTHORIZE_URL = "https://login.dingtalk.com/oauth2/auth"
_DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
_DINGTALK_USER_URL = "https://api.dingtalk.com/v1.0/contact/users/me"
_MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024


class DingTalkOAuthError(RuntimeError):
    """Sanitized DingTalk OAuth failure safe to surface as an auth error."""


@dataclass(frozen=True)
class DingTalkOAuthProvider:
    client_id: str
    client_secret: str
    proxy: SharedProxyConfig | None = None
    timeout_seconds: float = 10.0

    def authorization_url(self, *, callback_url: str, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "redirect_uri": callback_url,
                "response_type": "code",
                "client_id": self.client_id,
                "scope": "openid",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"{_DINGTALK_AUTHORIZE_URL}?{query}"

    async def authenticate(self, *, auth_code: str) -> Principal:
        return await asyncio.to_thread(self._authenticate_sync, auth_code=auth_code)

    def _opener(self) -> urllib.request.OpenerDirector:
        if self.proxy is None:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {
                    "http": self.proxy.url,
                    "https": self.proxy.url,
                }
            )
        )

    def _authenticate_sync(self, *, auth_code: str) -> Principal:
        if not auth_code:
            raise DingTalkOAuthError("missing DingTalk authorization response")
        opener = self._opener()
        token_payload = self._exchange_code(opener, auth_code=auth_code)
        access_token = token_payload.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise DingTalkOAuthError("DingTalk token response did not contain an access token")
        user = self._fetch_user(opener, access_token)
        union_id = user.get("unionId")
        if not isinstance(union_id, str) or not union_id.strip():
            raise DingTalkOAuthError("DingTalk user response did not contain a stable unionId")
        return principal_from_parts("dingtalk", union_id.strip())

    def _exchange_code(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        auth_code: str,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
                "code": auth_code,
                "grantType": "authorization_code",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            _DINGTALK_TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        return self._request_json(opener, request, "DingTalk token exchange failed")

    def _fetch_user(
        self,
        opener: urllib.request.OpenerDirector,
        access_token: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            _DINGTALK_USER_URL,
            method="GET",
            headers={
                "Accept": "application/json",
                "x-acs-dingtalk-access-token": access_token,
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        return self._request_json(opener, request, "DingTalk user lookup failed")

    def _request_json(
        self,
        opener: urllib.request.OpenerDirector,
        request: urllib.request.Request,
        failure_message: str,
    ) -> dict[str, Any]:
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise DingTalkOAuthError(failure_message)
                body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except DingTalkOAuthError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DingTalkOAuthError(failure_message) from exc
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise DingTalkOAuthError(failure_message)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DingTalkOAuthError(failure_message) from exc
        if not isinstance(payload, dict):
            raise DingTalkOAuthError(failure_message)
        return payload
