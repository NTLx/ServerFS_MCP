from __future__ import annotations

import asyncio

import pytest

from serverfs_mcp.http_auth import StaticAPIKeyMiddleware


async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _scope(*, headers: list[tuple[bytes, bytes]] | None = None, query: bytes = b"") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": query,
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("mcp.example.com", 443),
    }


def _run(
    middleware: StaticAPIKeyMiddleware,
    scope: dict,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    downstream_calls: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []

    async def app_receive() -> dict[str, object]:
        return await _receive()

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def runner() -> None:
        await middleware(scope, app_receive, send)

    async def downstream(scope, receive, send) -> None:
        downstream_calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware._app = downstream  # type: ignore[reportPrivateUsage]
    asyncio.run(runner())
    return downstream_calls, sent


def _middleware() -> StaticAPIKeyMiddleware:
    async def placeholder(scope, receive, send) -> None:
        raise AssertionError("placeholder app should be replaced by test helper")

    return StaticAPIKeyMiddleware(placeholder, "test-secret")


def test_correct_bearer_reaches_mcp_app() -> None:
    calls, sent = _run(
        _middleware(),
        _scope(headers=[(b"authorization", b"Bearer test-secret")]),
    )
    assert len(calls) == 1
    assert sent[0]["status"] == 204


def test_bearer_scheme_is_case_insensitive() -> None:
    calls, sent = _run(
        _middleware(),
        _scope(headers=[(b"authorization", b"bEaReR test-secret")]),
    )
    assert len(calls) == 1
    assert sent[0]["status"] == 204


def test_correct_query_key_reaches_mcp_app() -> None:
    calls, sent = _run(_middleware(), _scope(query=b"key=test-secret"))
    assert len(calls) == 1
    assert sent[0]["status"] == 204


def test_query_key_is_percent_decoded_once() -> None:
    middleware = _middleware()
    middleware._api_key = "abc+123"  # type: ignore[reportPrivateUsage]
    calls, sent = _run(middleware, _scope(query=b"key=abc%2B123"))
    assert len(calls) == 1
    assert sent[0]["status"] == 204


@pytest.mark.parametrize(
    "scope",
    [
        _scope(),
        _scope(headers=[(b"authorization", b"Bearer wrong-secret")]),
        _scope(headers=[(b"authorization", b"Basic test-secret")]),
        _scope(headers=[(b"authorization", b"Bearer")]),
        _scope(query=b"key="),
        _scope(query=b"key=wrong-secret"),
        _scope(query=b"key=test-secret&key=test-secret"),
        _scope(
            headers=[(b"authorization", b"Bearer test-secret")],
            query=b"key=test-secret",
        ),
        _scope(
            headers=[
                (b"authorization", b"Bearer test-secret"),
                (b"authorization", b"Bearer test-secret"),
            ]
        ),
    ],
)
def test_missing_wrong_malformed_or_ambiguous_credentials_are_rejected(scope: dict) -> None:
    calls, sent = _run(_middleware(), scope)
    assert calls == []
    assert sent[0]["status"] == 401
    assert (b"cache-control", b"no-store") in sent[0]["headers"]
    assert b"test-secret" not in sent[-1].get("body", b"")


def test_empty_configured_key_is_rejected_at_construction() -> None:
    async def app(scope, receive, send) -> None:
        return None

    with pytest.raises(ValueError, match="non-empty"):
        StaticAPIKeyMiddleware(app, "")
