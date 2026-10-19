"""WeChat Open Platform website-login adapter for ``serverfs-auth``."""

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

_WECHAT_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/qrconnect"
_WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
_WECHAT_USER_URL = "https://api.weixin.qq.com/sns/userinfo"
_MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024


class WeChatOAuthError(RuntimeError):
    """Sanitized WeChat OAuth failure safe to surface as an auth error."""


@dataclass(frozen=True)
class WeChatOAuthProvider:
    client_id: str
    client_secret: str
    proxy: SharedProxyConfig | None = None
    timeout_seconds: float = 10.0

    def authorization_url(self, *, callback_url: str, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "appid": self.client_id,
                "redirect_uri": callback_url,
                "response_type": "code",
                "scope": "snsapi_login",
                "state": state,
            }
        )
        return f"{_WECHAT_AUTHORIZE_URL}?{query}#wechat_redirect"

    async def authenticate(self, *, code: str) -> Principal:
        return await asyncio.to_thread(self._authenticate_sync, code=code)

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

    def _authenticate_sync(self, *, code: str) -> Principal:
        if not code:
            raise WeChatOAuthError("missing WeChat authorization response")
        opener = self._opener()
        token_payload = self._exchange_code(opener, code=code)
        self._reject_provider_error(token_payload, "WeChat rejected the authorization code")
        access_token = token_payload.get("access_token")
        open_id = token_payload.get("openid")
        if not isinstance(access_token, str) or not access_token:
            raise WeChatOAuthError("WeChat token response did not contain an access token")
        if not isinstance(open_id, str) or not open_id:
            raise WeChatOAuthError("WeChat token response did not contain an openid")

        user = self._fetch_user(opener, access_token=access_token, open_id=open_id)
        self._reject_provider_error(user, "WeChat user lookup failed")
        if user.get("openid") != open_id:
            raise WeChatOAuthError("WeChat user response openid did not match the token subject")
        union_id = user.get("unionid")
        if not isinstance(union_id, str) or not union_id.strip():
            raise WeChatOAuthError("WeChat user response did not contain a stable unionid")
        return principal_from_parts("wechat", union_id.strip())

    def _exchange_code(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        code: str,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "appid": self.client_id,
                "secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            }
        )
        request = urllib.request.Request(
            f"{_WECHAT_TOKEN_URL}?{query}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "ServerFS-OAuth/0.16"},
        )
        return self._request_json(opener, request, "WeChat token exchange failed")

    def _fetch_user(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        access_token: str,
        open_id: str,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "access_token": access_token,
                "openid": open_id,
            }
        )
        request = urllib.request.Request(
            f"{_WECHAT_USER_URL}?{query}",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "ServerFS-OAuth/0.16"},
        )
        return self._request_json(opener, request, "WeChat user lookup failed")

    @staticmethod
    def _reject_provider_error(payload: dict[str, Any], message: str) -> None:
        errcode = payload.get("errcode")
        if errcode not in {None, 0}:
            raise WeChatOAuthError(message)

    def _request_json(
        self,
        opener: urllib.request.OpenerDirector,
        request: urllib.request.Request,
        failure_message: str,
    ) -> dict[str, Any]:
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise WeChatOAuthError(failure_message)
                body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except WeChatOAuthError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WeChatOAuthError(failure_message) from exc
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise WeChatOAuthError(failure_message)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeChatOAuthError(failure_message) from exc
        if not isinstance(payload, dict):
            raise WeChatOAuthError(failure_message)
        return payload
