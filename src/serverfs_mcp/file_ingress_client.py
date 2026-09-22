"""Bounded client for the isolated ServerFS file-ingress sidecar."""

from __future__ import annotations

import http.client
import json

from .binary import BinaryTransferError

_MAX_ERROR_BODY = 4096
_INGRESS_HOST = "serverfs-file-ingress"
_INGRESS_PORT = 8081
_INGRESS_PATH = "/fetch"


class FileIngressClient:
    """Fetch ChatGPT-provided file bytes through the fixed internal ingress service."""

    def __init__(self, *, timeout_seconds: float = 30.0):
        self._timeout_seconds = timeout_seconds

    def fetch(self, download_url: str, *, max_bytes: int) -> bytes:
        """Return raw bytes, enforcing the workdir byte ceiling again client-side."""
        body = json.dumps(
            {"download_url": download_url, "max_bytes": max_bytes},
            separators=(",", ":"),
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            _INGRESS_HOST,
            _INGRESS_PORT,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                _INGRESS_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/octet-stream",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                code = self._read_error_code(response)
                if code == "BINARY_PAYLOAD_TOO_LARGE":
                    raise BinaryTransferError(
                        code,
                        f"payload exceeds the {max_bytes}-byte binary transfer limit",
                    )
                raise BinaryTransferError(
                    code,
                    "file ingress rejected the temporary file",
                )

            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = -1
                if declared > max_bytes:
                    raise BinaryTransferError(
                        "BINARY_PAYLOAD_TOO_LARGE",
                        f"payload exceeds the {max_bytes}-byte binary transfer limit",
                    )
            data = response.read(max_bytes + 1)
        except BinaryTransferError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException):
            raise BinaryTransferError(
                "FILE_INGRESS_UNAVAILABLE",
                "file ingress service is unavailable",
            ) from None
        finally:
            connection.close()

        if len(data) > max_bytes:
            raise BinaryTransferError(
                "BINARY_PAYLOAD_TOO_LARGE",
                f"payload exceeds the {max_bytes}-byte binary transfer limit",
            )
        return data

    @staticmethod
    def _read_error_code(response: http.client.HTTPResponse) -> str:
        try:
            raw = response.read(_MAX_ERROR_BODY)
            payload = json.loads(raw.decode("utf-8"))
            code = payload.get("code")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            code = None
        if isinstance(code, str) and code.isupper() and code.replace("_", "").isalnum():
            return code
        return "FILE_INGRESS_FAILED"
