"""v0.5 MCP-to-sidecar file-ingress client tests."""

from __future__ import annotations

import json

import pytest

import serverfs_mcp.file_ingress_client as client_module
from serverfs_mcp.binary import BinaryTransferError
from serverfs_mcp.file_ingress_client import FileIngressClient


class _Response:
    def __init__(
        self,
        data: bytes,
        *,
        status: int = 200,
        content_length: str | None = None,
    ):
        self.status = status
        self._data = data

        self._content_length = content_length

    def getheader(self, name: str):
        return self._content_length if name == "Content-Length" else None

    def read(self, amount: int) -> bytes:
        return self._data[:amount]


class _Connection:
    response = _Response(b"")
    instances: list[_Connection] = []

    def __init__(self, host: str, port: int, timeout: float):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_call = None
        self.closed = False
        self.instances.append(self)

    def request(self, method: str, path: str, *, body: bytes, headers: dict) -> None:
        self.request_call = (method, path, body, headers)

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch, response: _Response) -> _Connection:
    _Connection.response = response
    _Connection.instances = []
    monkeypatch.setattr(client_module.http.client, "HTTPConnection", _Connection)
    return _Connection


def test_fetch_posts_only_to_fixed_internal_endpoint(monkeypatch) -> None:
    _install(monkeypatch, _Response(b"PNG", content_length="3"))
    client = FileIngressClient(timeout_seconds=7)
    assert client.fetch("https://files.example.com/x?secret=signed", max_bytes=100) == b"PNG"

    connection = _Connection.instances[0]
    assert (connection.host, connection.port, connection.timeout) == (
        "serverfs-file-ingress",
        8081,
        7,
    )
    method, path, body, _headers = connection.request_call
    assert method == "POST"
    assert path == "/fetch"
    assert json.loads(body.decode("utf-8")) == {
        "download_url": "https://files.example.com/x?secret=signed",
        "max_bytes": 100,
    }
    assert connection.closed is True


def test_fetch_rejects_oversized_declared_response(monkeypatch) -> None:
    _install(monkeypatch, _Response(b"x", content_length="101"))
    with pytest.raises(BinaryTransferError) as exc:
        FileIngressClient().fetch("https://files.example.com/x", max_bytes=100)
    assert exc.value.code == "BINARY_PAYLOAD_TOO_LARGE"
    assert _Connection.instances[0].closed is True


def test_fetch_rejects_oversized_streamed_response(monkeypatch) -> None:
    _install(monkeypatch, _Response(b"x" * 101))
    with pytest.raises(BinaryTransferError) as exc:
        FileIngressClient().fetch("https://files.example.com/x", max_bytes=100)
    assert exc.value.code == "BINARY_PAYLOAD_TOO_LARGE"


def test_sidecar_error_code_is_mapped_without_reflecting_url(monkeypatch) -> None:
    body = json.dumps({"code": "FILE_INGRESS_HOST_NOT_ALLOWED"}).encode()
    _install(monkeypatch, _Response(body, status=400))
    with pytest.raises(BinaryTransferError) as exc:
        FileIngressClient().fetch("https://secret.example/x?token=never-reflect", max_bytes=100)
    assert exc.value.code == "FILE_INGRESS_HOST_NOT_ALLOWED"
    assert "secret.example" not in exc.value.message
    assert "token" not in exc.value.message


def test_connection_failure_is_coded_without_reflecting_url(monkeypatch) -> None:
    class BrokenConnection(_Connection):
        def request(self, *args, **kwargs) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(client_module.http.client, "HTTPConnection", BrokenConnection)
    with pytest.raises(BinaryTransferError) as exc:
        FileIngressClient().fetch("https://secret.example/x?token=never-reflect", max_bytes=100)
    assert exc.value.code == "FILE_INGRESS_UNAVAILABLE"
    assert "secret.example" not in exc.value.message
    assert "token" not in exc.value.message
