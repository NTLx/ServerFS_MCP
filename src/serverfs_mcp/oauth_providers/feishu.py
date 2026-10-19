"""Feishu OAuth identity adapter for ``serverfs-auth``."""

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

_FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
_FEISHU_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
_FEISHU_USER_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
_FEISHU_IDENTITY_SCOPE = "contact:user.base:readonly"
_MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024


class FeishuOAuthError(RuntimeError):
    """Sanitized Feishu OAuth failure safe to surface as an auth error."""


@dataclass(frozen=True)
class FeishuOAuthProvider:
    client_id: str
    client_secret: str
    proxy: SharedProxyConfig | None = None
    timeout_seconds: float = 10.0

    def authorization_url(self, *, callback_url: str, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": callback_url,
                "scope": _FEISHU_IDENTITY_SCOPE,
                "state": state,
            }
        )
        return f"{_FEISHU_AUTHORIZE_URL}?{query}"

    async def authenticate(self, *, code: str, callback_url: str) -> Principal:
        return await asyncio.to_thread(
            self._authenticate_sync,
            code=code,
            callback_url=callback_url,
        )

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

    def _authenticate_sync(self, *, code: str, callback_url: str) -> Principal:
        if not code:
            raise FeishuOAuthError("missing Feishu authorization response")
        opener = self._opener()
        token_payload = self._exchange_code(opener, code=code, callback_url=callback_url)
        if token_payload.get("code") not in {None, 0}:
            raise FeishuOAuthError("Feishu rejected the authorization code")
        access_token = token_payload.get("access_token")
        token_type = token_payload.get("token_type")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuOAuthError("Feishu token response did not contain an access token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise FeishuOAuthError("Feishu token response used an unsupported token type")

        user_payload = self._fetch_user(opener, access_token)
        if user_payload.get("code") != 0:
            raise FeishuOAuthError("Feishu user lookup failed")
        data = user_payload.get("data")
        if not isinstance(data, dict):
            raise FeishuOAuthError("Feishu user response did not contain identity data")
        open_id = data.get("open_id")
        if not isinstance(open_id, str) or not open_id.startswith("ou_") or len(open_id) <= 3:
            raise FeishuOAuthError("Feishu user response did not contain a stable open_id")
        return principal_from_parts("feishu", open_id)

    def _exchange_code(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        code: str,
        callback_url: str,
    ) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": callback_url,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            _FEISHU_TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        return self._request_json(opener, request, "Feishu token exchange failed")

    def _fetch_user(
        self,
        opener: urllib.request.OpenerDirector,
        access_token: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            _FEISHU_USER_URL,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        return self._request_json(opener, request, "Feishu user lookup failed")

    def _request_json(
        self,
        opener: urllib.request.OpenerDirector,
        request: urllib.request.Request,
        failure_message: str,
    ) -> dict[str, Any]:
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise FeishuOAuthError(failure_message)
                body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except FeishuOAuthError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeishuOAuthError(failure_message) from exc
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise FeishuOAuthError(failure_message)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuOAuthError(failure_message) from exc
        if not isinstance(payload, dict):
            raise FeishuOAuthError(failure_message)
        return payload
