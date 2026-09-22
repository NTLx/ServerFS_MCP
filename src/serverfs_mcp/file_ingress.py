"""Isolated HTTPS fetch sidecar for ChatGPT/OpenAI temporary file URLs."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

_READ_CHUNK = 64 * 1024
_MAX_REQUEST_BODY = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_OPENAI_BLOB_ACCOUNT_PREFIX = "oaisdmntpr"
_OPENAI_BLOB_SUFFIX = ".blob.core.windows.net"
_AZURE_STORAGE_ACCOUNT_MAX_LENGTH = 24


class IngressError(Exception):
    """Expected ingress failure with a stable public error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IngressSettings:
    allowed_hosts: frozenset[str]
    allow_openai_blob_hosts: bool = False
    max_bytes: int = 8_388_608
    timeout_seconds: float = 30.0
    max_redirects: int = 3
    listen_host: str = "0.0.0.0"
    listen_port: int = 8081


def _positive_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid positive integer {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"invalid positive integer {raw!r}")
    return value


def _positive_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid positive number {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"invalid positive number {raw!r}")
    return value


def _strict_bool(raw: str | None, name: str, default: bool = False) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _normalize_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError(f"invalid hostname {host!r}") from exc


def settings_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> IngressSettings:
    env = os.environ if env is None else env
    hosts = frozenset(
        _normalize_host(item.strip())
        for item in env.get("SERVERFS_FILE_INGRESS_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    if any("*" in host for host in hosts):
        raise ValueError("SERVERFS_FILE_INGRESS_ALLOWED_HOSTS does not support wildcards")
    allow_openai_blob_hosts = _strict_bool(
        env.get("SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS"),
        "SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS",
    )
    if not hosts and not allow_openai_blob_hosts:
        raise ValueError(
            "file ingress requires exact allowed hosts or the constrained OpenAI Blob host family"
        )
    return IngressSettings(
        allowed_hosts=hosts,
        allow_openai_blob_hosts=allow_openai_blob_hosts,
        max_bytes=_positive_int(env.get("SERVERFS_FILE_INGRESS_MAX_BYTES"), 8_388_608),
        timeout_seconds=_positive_float(
            env.get("SERVERFS_FILE_INGRESS_FETCH_TIMEOUT_SECONDS"), 30.0
        ),
        max_redirects=_positive_int(env.get("SERVERFS_FILE_INGRESS_MAX_REDIRECTS"), 3),
        listen_port=_positive_int(env.get("SERVERFS_FILE_INGRESS_PORT"), 8081),
    )


def _is_openai_blob_host(host: str) -> bool:
    """Match only the measured OpenAI-managed Azure Blob account-name family."""
    if not host.endswith(_OPENAI_BLOB_SUFFIX):
        return False
    account = host[: -len(_OPENAI_BLOB_SUFFIX)]
    if not account.startswith(_OPENAI_BLOB_ACCOUNT_PREFIX):
        return False
    if len(account) <= len(_OPENAI_BLOB_ACCOUNT_PREFIX):
        return False
    if len(account) > _AZURE_STORAGE_ACCOUNT_MAX_LENGTH:
        return False
    return account.isascii() and account.isalnum() and account == account.lower()


def _host_allowed(host: str, settings: IngressSettings) -> bool:
    if host in settings.allowed_hosts:
        return True
    return settings.allow_openai_blob_hosts and _is_openai_blob_host(host)


def _validated_url(url: str, settings: IngressSettings) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "invalid URL") from None
    if parsed.scheme.lower() != "https":
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "only HTTPS URLs are accepted")
    if not parsed.hostname or parsed.username or parsed.password:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "URL authority is not allowed")
    if parsed.fragment:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "URL fragments are not accepted")
    try:
        port = parsed.port or 443
    except ValueError:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "invalid URL port") from None
    if port != 443:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "only HTTPS port 443 is accepted")
    try:
        host = _normalize_host(parsed.hostname)
    except ValueError:
        raise IngressError("FILE_INGRESS_URL_NOT_ALLOWED", "invalid URL hostname") from None
    if not _host_allowed(host, settings):
        raise IngressError("FILE_INGRESS_HOST_NOT_ALLOWED", "URL host is not allowlisted")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return host, port, target


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise IngressError("FILE_INGRESS_DNS_FAILED", "allowed host did not resolve") from None

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise IngressError("FILE_INGRESS_DNS_FAILED", "host resolved unexpectedly") from None
        if not ip.is_global:
            raise IngressError(
                "FILE_INGRESS_ADDRESS_NOT_ALLOWED", "host resolved to a non-global address"
            )
        canonical = str(ip)
        if canonical not in addresses:
            addresses.append(canonical)
    if not addresses:
        raise IngressError("FILE_INGRESS_DNS_FAILED", "allowed host did not resolve")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a prevalidated IP while verifying the original host."""

    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(
            host=host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _request_once(
    url: str, settings: IngressSettings, *, max_bytes: int
) -> tuple[int, str | None, bytes | None]:
    host, port, target = _validated_url(url, settings)
    addresses = _resolve_public_addresses(host, port)
    last_error: OSError | None = None

    for address in addresses:
        connection = _PinnedHTTPSConnection(host, address, port, settings.timeout_seconds)
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Host": host,
                    "User-Agent": "ServerFS-File-Ingress/0.5",
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = response.status
            location = response.getheader("Location")
            if status in _REDIRECT_STATUSES:
                return status, location, None
            if status != 200:
                raise IngressError("FILE_INGRESS_UPSTREAM_FAILED", "temporary file fetch failed")

            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = -1
                if declared > max_bytes:
                    raise IngressError(
                        "BINARY_PAYLOAD_TOO_LARGE", "temporary file exceeds the byte limit"
                    )

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(_READ_CHUNK, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise IngressError(
                        "BINARY_PAYLOAD_TOO_LARGE", "temporary file exceeds the byte limit"
                    )
            return status, None, b"".join(chunks)
        except IngressError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()

    raise IngressError(
        "FILE_INGRESS_UPSTREAM_FAILED", "temporary file fetch failed"
    ) from last_error


def fetch_file(url: str, settings: IngressSettings, *, requested_max_bytes: int) -> bytes:
    if requested_max_bytes <= 0:
        raise IngressError("FILE_INGRESS_BAD_REQUEST", "max_bytes must be positive")
    max_bytes = min(requested_max_bytes, settings.max_bytes)
    current = url
    for redirects in range(settings.max_redirects + 1):
        status, location, data = _request_once(current, settings, max_bytes=max_bytes)
        if status == 200:
            assert data is not None
            return data
        if not location:
            raise IngressError("FILE_INGRESS_UPSTREAM_FAILED", "redirect has no location")
        if redirects >= settings.max_redirects:
            raise IngressError("FILE_INGRESS_TOO_MANY_REDIRECTS", "too many redirects")
        current = urljoin(current, location)
    raise IngressError("FILE_INGRESS_TOO_MANY_REDIRECTS", "too many redirects")


def _json_error(handler: BaseHTTPRequestHandler, status: int, code: str) -> None:
    body = json.dumps({"code": code}, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handler(settings: IngressSettings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ServerFSFileIngress/0.5"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path != "/healthz":
                _json_error(self, 404, "NOT_FOUND")
                return
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/fetch":
                _json_error(self, 404, "NOT_FOUND")
                return
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                _json_error(self, 400, "FILE_INGRESS_BAD_REQUEST")
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                _json_error(self, 400, "FILE_INGRESS_BAD_REQUEST")
                return
            if length <= 0 or length > _MAX_REQUEST_BODY:
                _json_error(self, 413, "FILE_INGRESS_BAD_REQUEST")
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                download_url = payload["download_url"]
                max_bytes = payload["max_bytes"]
                if not isinstance(download_url, str) or not isinstance(max_bytes, int):
                    raise TypeError
                data = fetch_file(download_url, settings, requested_max_bytes=max_bytes)
            except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                _json_error(self, 400, "FILE_INGRESS_BAD_REQUEST")
                return
            except IngressError as exc:
                status = 413 if exc.code == "BINARY_PAYLOAD_TOO_LARGE" else 502
                if exc.code in {
                    "FILE_INGRESS_URL_NOT_ALLOWED",
                    "FILE_INGRESS_HOST_NOT_ALLOWED",
                    "FILE_INGRESS_ADDRESS_NOT_ALLOWED",
                    "FILE_INGRESS_BAD_REQUEST",
                }:
                    status = 400
                _json_error(self, status, exc.code)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> int:
    try:
        settings = settings_from_env()
    except ValueError as exc:
        raise SystemExit(f"ServerFS file ingress: configuration error: {exc}") from exc
    server = ThreadingHTTPServer(
        (settings.listen_host, settings.listen_port),
        _handler(settings),
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
