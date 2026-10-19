"""Optional OAuth Authorization Server / identity-broker process for ServerFS."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import urllib.parse
from typing import Any

import uvicorn
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import logging as jsonlog
from .auth_clients import ClientMetadataResolver, load_pre_registered_clients
from .auth_config import AuthServiceSettings, auth_settings_from_env
from .auth_policy import AdmissionPolicy, Principal, parse_principal
from .auth_provider import ServerFSOAuthProvider
from .auth_store import OAuthStateStore
from .oauth_providers.dingtalk import DingTalkOAuthError, DingTalkOAuthProvider
from .oauth_providers.feishu import FeishuOAuthError, FeishuOAuthProvider
from .oauth_providers.github import GitHubOAuthError, GitHubOAuthProvider
from .oauth_providers.wechat import WeChatOAuthError, WeChatOAuthProvider

_MAX_INTROSPECTION_BODY_BYTES = 4096


def create_auth_app(
    settings: AuthServiceSettings,
    *,
    provider: ServerFSOAuthProvider | None = None,
    client_resolver: ClientMetadataResolver | None = None,
    github_provider: GitHubOAuthProvider | None = None,
    feishu_provider: FeishuOAuthProvider | None = None,
    dingtalk_provider: DingTalkOAuthProvider | None = None,
    wechat_provider: WeChatOAuthProvider | None = None,
    test_identity: str | None = None,
) -> Starlette:
    """Build the isolated authorization service.

    ``test_identity`` exists only for deterministic local tests. It is never
    populated from environment configuration and therefore cannot enable a
    fake-login path in the production CLI.
    """

    if not settings.oauth_enabled:
        raise ValueError("serverfs-auth requires SERVERFS_OAUTH_ENABLED=true")
    if not settings.public_url or not settings.resource_url:
        raise ValueError("serverfs-auth requires public issuer and resource URLs")

    store = OAuthStateStore(settings.state_dir)
    store.initialize()
    oauth_provider = provider or ServerFSOAuthProvider(
        store=store,
        issuer_url=settings.public_url,
        resource_url=settings.resource_url,
        supported_scopes=("serverfs:access",),
        access_token_ttl_seconds=settings.access_token_ttl_seconds,
        refresh_token_ttl_seconds=settings.refresh_token_ttl_seconds,
        client_resolver=client_resolver or ClientMetadataResolver(),
    )
    for client in load_pre_registered_clients(settings.clients_file):
        oauth_provider.pre_register_client(client)
    client_authenticator = ClientAuthenticator(oauth_provider)
    admission_policy = AdmissionPolicy(settings.allowed_principals)
    test_principal = parse_principal(test_identity) if test_identity is not None else None
    effective_github_provider = github_provider
    if effective_github_provider is None and settings.github_enabled:
        effective_github_provider = GitHubOAuthProvider(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            proxy=settings.proxy if settings.use_proxy else None,
        )
    effective_feishu_provider = feishu_provider
    if effective_feishu_provider is None and settings.feishu_enabled:
        effective_feishu_provider = FeishuOAuthProvider(
            client_id=settings.feishu_client_id,
            client_secret=settings.feishu_client_secret,
            proxy=settings.proxy if settings.use_proxy else None,
        )
    effective_dingtalk_provider = dingtalk_provider
    if effective_dingtalk_provider is None and settings.dingtalk_enabled:
        effective_dingtalk_provider = DingTalkOAuthProvider(
            client_id=settings.dingtalk_client_id,
            client_secret=settings.dingtalk_client_secret,
            proxy=settings.proxy if settings.use_proxy else None,
        )
    effective_wechat_provider = wechat_provider
    if effective_wechat_provider is None and settings.wechat_enabled:
        effective_wechat_provider = WeChatOAuthProvider(
            client_id=settings.wechat_client_id,
            client_secret=settings.wechat_client_secret,
            proxy=settings.proxy if settings.use_proxy else None,
        )
    github_callback_url = f"{settings.public_url}/oauth/provider/github/callback"
    feishu_callback_url = f"{settings.public_url}/oauth/provider/feishu/callback"
    dingtalk_callback_url = f"{settings.public_url}/oauth/provider/dingtalk/callback"
    wechat_callback_url = f"{settings.public_url}/oauth/provider/wechat/callback"

    def admit(principal: Principal) -> bool:
        allowed = admission_policy.allows(principal)
        jsonlog.info(
            "auth_admission_allowed" if allowed else "auth_admission_denied",
            provider=principal.provider,
            principal_id=principal.audit_id,
        )
        return allowed

    def client_redirect(completed) -> Response:
        redirect = construct_redirect_uri(
            str(completed.code.redirect_uri),
            code=completed.code.code,
            state=completed.state,
            iss=settings.public_url,
        )
        return RedirectResponse(
            redirect,
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def complete_upstream_identity(upstream, principal: Principal) -> Response:
        if not admit(principal):
            store.discard_pending_authorization_hash(upstream.pending_authorization_hash)
            return JSONResponse({"error": "access_denied"}, status_code=403)
        completed = store.complete_pending_authorization_hash(
            upstream.pending_authorization_hash,
            subject=principal.canonical,
        )
        if completed is None:
            return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
        return client_redirect(completed)

    async def login(request: Request) -> Response:
        transaction = request.query_params.get("transaction", "")
        if not transaction:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if test_principal is not None:
            if not admit(test_principal):
                return JSONResponse({"error": "access_denied"}, status_code=403)
            completed = oauth_provider.complete_authorization(
                transaction, subject=test_principal.canonical
            )
            if completed is None:
                return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
            return client_redirect(completed)

        providers: list[tuple[str, str]] = []
        if effective_github_provider is not None:
            providers.append(("GitHub", "github"))
        if effective_feishu_provider is not None:
            providers.append(("Feishu", "feishu"))
        if effective_dingtalk_provider is not None:
            providers.append(("DingTalk", "dingtalk"))
        if effective_wechat_provider is not None:
            providers.append(("WeChat", "wechat"))
        if not providers:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        query = urllib.parse.urlencode({"transaction": transaction})
        if len(providers) == 1:
            start = f"/oauth/provider/{providers[0][1]}/start?{query}"
            return RedirectResponse(start, status_code=302, headers={"Cache-Control": "no-store"})

        links = "".join(
            f'<li><a href="/oauth/provider/{slug}/start?{query}">{label}</a></li>'
            for label, slug in providers
        )
        return HTMLResponse(
            f"<!doctype html><html><head><title>Sign in to ServerFS</title></head>"
            f"<body><main><h1>Sign in to ServerFS</h1><ul>{links}</ul></main></body></html>",
            headers={"Cache-Control": "no-store"},
        )

    async def github_start(request: Request) -> Response:
        if effective_github_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        transaction = request.query_params.get("transaction", "")
        if not transaction:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        state = store.create_upstream_login(
            transaction,
            provider="github",
            code_verifier=code_verifier,
        )
        if state is None:
            return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
        redirect = effective_github_provider.authorization_url(
            callback_url=github_callback_url,
            state=state,
            code_challenge=code_challenge,
        )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    async def github_callback(request: Request) -> Response:
        if effective_github_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        state = request.query_params.get("state", "")
        if not state:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        upstream = store.consume_upstream_login(state, provider="github")
        if upstream is None:
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)
        if request.query_params.get("error"):
            return JSONResponse({"error": "access_denied"}, status_code=400)
        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            principal = await effective_github_provider.authenticate(
                code=code,
                callback_url=github_callback_url,
                code_verifier=upstream.code_verifier,
            )
        except GitHubOAuthError:
            jsonlog.warning("auth_provider_failed", provider="github")
            return JSONResponse({"error": "identity_provider_failed"}, status_code=502)
        return complete_upstream_identity(upstream, principal)

    async def feishu_start(request: Request) -> Response:
        if effective_feishu_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        transaction = request.query_params.get("transaction", "")
        if not transaction:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        state = store.create_upstream_login(
            transaction,
            provider="feishu",
            code_verifier="",
        )
        if state is None:
            return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
        redirect = effective_feishu_provider.authorization_url(
            callback_url=feishu_callback_url,
            state=state,
        )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    async def feishu_callback(request: Request) -> Response:
        if effective_feishu_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        state = request.query_params.get("state", "")
        if not state:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        upstream = store.consume_upstream_login(state, provider="feishu")
        if upstream is None:
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)
        if request.query_params.get("error"):
            return JSONResponse({"error": "access_denied"}, status_code=400)
        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            principal = await effective_feishu_provider.authenticate(
                code=code,
                callback_url=feishu_callback_url,
            )
        except FeishuOAuthError:
            jsonlog.warning("auth_provider_failed", provider="feishu")
            return JSONResponse({"error": "identity_provider_failed"}, status_code=502)
        return complete_upstream_identity(upstream, principal)

    async def dingtalk_start(request: Request) -> Response:
        if effective_dingtalk_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        transaction = request.query_params.get("transaction", "")
        if not transaction:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        state = store.create_upstream_login(
            transaction,
            provider="dingtalk",
            code_verifier="",
        )
        if state is None:
            return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
        redirect = effective_dingtalk_provider.authorization_url(
            callback_url=dingtalk_callback_url,
            state=state,
        )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    async def dingtalk_callback(request: Request) -> Response:
        if effective_dingtalk_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        state = request.query_params.get("state", "")
        if not state:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        upstream = store.consume_upstream_login(state, provider="dingtalk")
        if upstream is None:
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)
        if request.query_params.get("error"):
            return JSONResponse({"error": "access_denied"}, status_code=400)
        auth_code = request.query_params.get("authCode", "")
        if not auth_code:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            principal = await effective_dingtalk_provider.authenticate(auth_code=auth_code)
        except DingTalkOAuthError:
            jsonlog.warning("auth_provider_failed", provider="dingtalk")
            return JSONResponse({"error": "identity_provider_failed"}, status_code=502)
        return complete_upstream_identity(upstream, principal)

    async def wechat_start(request: Request) -> Response:
        if effective_wechat_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        transaction = request.query_params.get("transaction", "")
        if not transaction:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        state = store.create_upstream_login(
            transaction,
            provider="wechat",
            code_verifier="",
        )
        if state is None:
            return JSONResponse({"error": "invalid_or_expired_transaction"}, status_code=400)
        redirect = effective_wechat_provider.authorization_url(
            callback_url=wechat_callback_url,
            state=state,
        )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    async def wechat_callback(request: Request) -> Response:
        if effective_wechat_provider is None:
            return JSONResponse({"error": "identity_provider_unavailable"}, status_code=503)
        state = request.query_params.get("state", "")
        if not state:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        upstream = store.consume_upstream_login(state, provider="wechat")
        if upstream is None:
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)
        if request.query_params.get("error"):
            return JSONResponse({"error": "access_denied"}, status_code=400)
        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            principal = await effective_wechat_provider.authenticate(code=code)
        except WeChatOAuthError:
            jsonlog.warning("auth_provider_failed", provider="wechat")
            return JSONResponse({"error": "identity_provider_failed"}, status_code=502)
        return complete_upstream_identity(upstream, principal)

    async def introspect(request: Request) -> Response:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        presented = authorization[7:]
        if not hmac.compare_digest(
            presented.encode("utf-8"), settings.introspection_secret.encode("utf-8")
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_INTROSPECTION_BODY_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            payload: Any = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        token = payload["token"]
        if not token:
            return JSONResponse({"active": False}, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            store.introspect_access_token(token),
            headers={"Cache-Control": "no-store"},
        )

    async def revoke(request: Request) -> Response:
        try:
            client = await client_authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message},
                status_code=401,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token is required"},
                status_code=400,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        stored = await oauth_provider.load_access_token(token)
        if stored is None:
            stored = await oauth_provider.load_refresh_token(client, token)
        if stored is not None and stored.client_id == client.client_id:
            await oauth_provider.revoke_token(stored)
        return Response(
            status_code=200,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    issuer_url = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=None,
    ).issuer_url
    registration_options = ClientRegistrationOptions(
        enabled=settings.dcr_enabled,
        valid_scopes=["serverfs:access"],
        default_scopes=["serverfs:access"],
    )
    revocation_options = RevocationOptions(enabled=True)
    metadata = build_metadata(
        issuer_url,
        service_documentation_url=None,
        client_registration_options=registration_options,
        revocation_options=revocation_options,
    )
    metadata.client_id_metadata_document_supported = True
    metadata.authorization_response_iss_parameter_supported = True
    auth_routes = create_auth_routes(
        provider=oauth_provider,
        issuer_url=issuer_url,
        client_registration_options=registration_options,
        revocation_options=revocation_options,
    )
    routes = [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        Route("/login", login, methods=["GET"]),
        Route("/oauth/provider/github/start", github_start, methods=["GET"]),
        Route("/oauth/provider/github/callback", github_callback, methods=["GET"]),
        Route("/oauth/provider/feishu/start", feishu_start, methods=["GET"]),
        Route("/oauth/provider/feishu/callback", feishu_callback, methods=["GET"]),
        Route("/oauth/provider/dingtalk/start", dingtalk_start, methods=["GET"]),
        Route("/oauth/provider/dingtalk/callback", dingtalk_callback, methods=["GET"]),
        Route("/oauth/provider/wechat/start", wechat_start, methods=["GET"]),
        Route("/oauth/provider/wechat/callback", wechat_callback, methods=["GET"]),
        Route("/internal/introspect", introspect, methods=["POST"]),
        Route("/revoke", revoke, methods=["POST"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
    routes.extend(
        route
        for route in auth_routes
        if getattr(route, "path", None)
        not in {"/.well-known/oauth-authorization-server", "/revoke"}
    )
    app = Starlette(routes=routes)
    app.state.oauth_store = store
    app.state.oauth_provider = oauth_provider
    app.state.admission_policy = admission_policy
    return app


def main() -> int:
    try:
        settings = auth_settings_from_env(os.environ)
        if not settings.oauth_enabled:
            raise ValueError("SERVERFS_OAUTH_ENABLED must be true to run serverfs-auth")
        app = create_auth_app(settings)
    except (ValueError, RuntimeError) as exc:
        jsonlog.error("auth_startup_failed", reason=str(exc))
        sys.stderr.write(f"ServerFS Auth: configuration error: {exc}\n")
        return 2

    jsonlog.info(
        "auth_startup",
        host=settings.host,
        port=settings.port,
        provider_count=len(settings.enabled_providers),
        admitted_principal_count=len(settings.allowed_principals),
        dcr_enabled=settings.dcr_enabled,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0
