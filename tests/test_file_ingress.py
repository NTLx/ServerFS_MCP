"""v0.5 isolated file-ingress security tests."""

from __future__ import annotations

import socket

import pytest

import serverfs_mcp.file_ingress as ingress
from serverfs_mcp.file_ingress import IngressError, IngressSettings


def _settings(*hosts: str, max_bytes: int = 8_388_608) -> IngressSettings:
    return IngressSettings(allowed_hosts=frozenset(hosts), max_bytes=max_bytes)


def test_settings_require_nonempty_exact_host_allowlist() -> None:
    with pytest.raises(ValueError):
        ingress.settings_from_env({})
    with pytest.raises(ValueError):
        ingress.settings_from_env({"SERVERFS_FILE_INGRESS_ALLOWED_HOSTS": "*.example.com"})


def test_settings_parse_exact_hosts_and_limits() -> None:
    settings = ingress.settings_from_env(
        {
            "SERVERFS_FILE_INGRESS_ALLOWED_HOSTS": "Files.Example.com,cdn.example.com",
            "SERVERFS_FILE_INGRESS_MAX_BYTES": "1234",
            "SERVERFS_FILE_INGRESS_FETCH_TIMEOUT_SECONDS": "4.5",
            "SERVERFS_FILE_INGRESS_MAX_REDIRECTS": "2",
        }
    )
    assert settings.allowed_hosts == frozenset({"files.example.com", "cdn.example.com"})
    assert settings.max_bytes == 1234
    assert settings.timeout_seconds == 4.5
    assert settings.max_redirects == 2


def test_validated_url_accepts_allowlisted_https_only() -> None:
    settings = _settings("files.example.com")
    host, port, target = ingress._validated_url(
        "https://files.example.com/path/image.png?sig=abc", settings
    )
    assert host == "files.example.com"
    assert port == 443
    assert target == "/path/image.png?sig=abc"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://files.example.com/a", "FILE_INGRESS_URL_NOT_ALLOWED"),
        ("https://user:pass@files.example.com/a", "FILE_INGRESS_URL_NOT_ALLOWED"),
        ("https://files.example.com:8443/a", "FILE_INGRESS_URL_NOT_ALLOWED"),
        ("https://files.example.com/a#fragment", "FILE_INGRESS_URL_NOT_ALLOWED"),
        ("https://evil.example/a", "FILE_INGRESS_HOST_NOT_ALLOWED"),
    ],
)
def test_validated_url_rejects_unsafe_shapes(url: str, code: str) -> None:
    with pytest.raises(IngressError) as exc:
        ingress._validated_url(url, _settings("files.example.com"))
    assert exc.value.code == code


def test_dns_rejects_private_address(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(IngressError) as exc:
        ingress._resolve_public_addresses("files.example.com", 443)
    assert exc.value.code == "FILE_INGRESS_ADDRESS_NOT_ALLOWED"


def test_dns_rejects_mixed_public_and_private_answers(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(IngressError) as exc:
        ingress._resolve_public_addresses("files.example.com", 443)
    assert exc.value.code == "FILE_INGRESS_ADDRESS_NOT_ALLOWED"


def test_dns_accepts_global_address(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert ingress._resolve_public_addresses("files.example.com", 443) == ["93.184.216.34"]


def test_redirect_target_is_revalidated(monkeypatch) -> None:
    settings = _settings("files.example.com")
    calls: list[str] = []

    def fake_request(url: str, cfg: IngressSettings, *, max_bytes: int):
        calls.append(url)
        ingress._validated_url(url, cfg)
        if len(calls) == 1:
            return 302, "https://evil.example/private", None
        raise AssertionError("unsafe redirect should fail validation before a second fetch")

    monkeypatch.setattr(ingress, "_request_once", fake_request)
    with pytest.raises(IngressError) as exc:
        ingress.fetch_file(
            "https://files.example.com/start",
            settings,
            requested_max_bytes=1024,
        )
    assert exc.value.code == "FILE_INGRESS_HOST_NOT_ALLOWED"
    assert calls == [
        "https://files.example.com/start",
        "https://evil.example/private",
    ]


def test_sidecar_limit_caps_requested_workdir_limit(monkeypatch) -> None:
    settings = _settings("files.example.com", max_bytes=1024)
    seen: list[int] = []

    def fake_request(url: str, cfg: IngressSettings, *, max_bytes: int):
        ingress._validated_url(url, cfg)
        seen.append(max_bytes)
        return 200, None, b"payload"

    monkeypatch.setattr(ingress, "_request_once", fake_request)
    assert (
        ingress.fetch_file(
            "https://files.example.com/file",
            settings,
            requested_max_bytes=4096,
        )
        == b"payload"
    )
    assert seen == [1024]


class _FakeResponse:
    def __init__(self, data: bytes, *, content_length: str | None = None):
        self.status = 200
        self._data = data
        self._offset = 0
        self._content_length = content_length

    def getheader(self, name: str):
        if name == "Content-Length":
            return self._content_length
        if name == "Location":
            return None
        return None

    def read(self, amount: int) -> bytes:
        chunk = self._data[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class _FakeConnection:
    response: _FakeResponse | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def request(self, *_args, **_kwargs) -> None:
        return

    def getresponse(self) -> _FakeResponse:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        return


def _install_fake_upstream(monkeypatch, response: _FakeResponse) -> None:
    _FakeConnection.response = response
    monkeypatch.setattr(
        ingress, "_resolve_public_addresses", lambda _host, _port: ["93.184.216.34"]
    )
    monkeypatch.setattr(ingress, "_PinnedHTTPSConnection", _FakeConnection)


def test_declared_oversize_is_rejected_before_body_read(monkeypatch) -> None:
    response = _FakeResponse(b"ignored", content_length="9")
    _install_fake_upstream(monkeypatch, response)
    with pytest.raises(IngressError) as exc:
        ingress._request_once(
            "https://files.example.com/file",
            _settings("files.example.com"),
            max_bytes=8,
        )
    assert exc.value.code == "BINARY_PAYLOAD_TOO_LARGE"
    assert response._offset == 0


def test_streaming_oversize_is_rejected_without_content_length(monkeypatch) -> None:
    _install_fake_upstream(monkeypatch, _FakeResponse(b"123456789"))
    with pytest.raises(IngressError) as exc:
        ingress._request_once(
            "https://files.example.com/file",
            _settings("files.example.com"),
            max_bytes=8,
        )
    assert exc.value.code == "BINARY_PAYLOAD_TOO_LARGE"


def test_exact_byte_limit_succeeds(monkeypatch) -> None:
    _install_fake_upstream(monkeypatch, _FakeResponse(b"12345678"))
    status, location, data = ingress._request_once(
        "https://files.example.com/file",
        _settings("files.example.com"),
        max_bytes=8,
    )
    assert status == 200
    assert location is None
    assert data == b"12345678"
