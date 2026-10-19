"""GitHub OAuth App identity adapter for ``serverfs-auth``."""

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

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024


class GitHubOAuthError(RuntimeError):
    """Sanitized GitHub OAuth failure safe to surface as an auth error."""


@dataclass(frozen=True)
class GitHubOAuthProvider:
    client_id: str
    client_secret: str
    proxy: SharedProxyConfig | None = None
    timeout_seconds: float = 10.0

    def authorization_url(
        self,
        *,
        callback_url: str,
        state: str,
        code_challenge: str,
    ) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": callback_url,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        # Deliberately omit `scope`: ServerFS needs only GitHub's public user
        # identity and must not request repository or private-user permissions.
        return f"{_GITHUB_AUTHORIZE_URL}?{query}"

    async def authenticate(
        self,
        *,
        code: str,
        callback_url: str,
        code_verifier: str,
    ) -> Principal:
        return await asyncio.to_thread(
            self._authenticate_sync,
            code=code,
            callback_url=callback_url,
            code_verifier=code_verifier,
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

    def _authenticate_sync(
        self,
        *,
        code: str,
        callback_url: str,
        code_verifier: str,
    ) -> Principal:
        if not code or not code_verifier:
            raise GitHubOAuthError("missing GitHub authorization response")
        opener = self._opener()
        token_payload = self._exchange_code(
            opener,
            code=code,
            callback_url=callback_url,
            code_verifier=code_verifier,
        )
        access_token = token_payload.get("access_token")
        token_type = token_payload.get("token_type")
        if not isinstance(access_token, str) or not access_token:
            raise GitHubOAuthError("GitHub token response did not contain an access token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise GitHubOAuthError("GitHub token response used an unsupported token type")
        user = self._fetch_user(opener, access_token)
        user_id = user.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise GitHubOAuthError("GitHub user response did not contain a stable numeric id")
        return principal_from_parts("github", str(user_id))

    def _exchange_code(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        code: str,
        callback_url: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": callback_url,
                "code_verifier": code_verifier,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            _GITHUB_TOKEN_URL,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        payload = self._request_json(opener, request, "GitHub token exchange failed")
        error = payload.get("error")
        if isinstance(error, str) and error:
            raise GitHubOAuthError("GitHub rejected the authorization code")
        return payload

    def _fetch_user(
        self,
        opener: urllib.request.OpenerDirector,
        access_token: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            _GITHUB_USER_URL,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "ServerFS-OAuth/0.16",
            },
        )
        return self._request_json(opener, request, "GitHub user lookup failed")

    def _request_json(
        self,
        opener: urllib.request.OpenerDirector,
        request: urllib.request.Request,
        failure_message: str,
    ) -> dict[str, Any]:
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise GitHubOAuthError(failure_message)
                body = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
        except GitHubOAuthError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubOAuthError(failure_message) from exc
        if len(body) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise GitHubOAuthError(failure_message)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubOAuthError(failure_message) from exc
        if not isinstance(payload, dict):
            raise GitHubOAuthError(failure_message)
        return payload
