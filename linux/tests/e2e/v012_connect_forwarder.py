"""Small credentialless CONNECT proxy used by Linux v0.12 live acceptance."""

from __future__ import annotations

import select
import socket
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ForwarderSummary:
    total_connects: int
    external_connects: int
    loopback_connects: int


class ConnectForwarder:
    """Relay HTTP CONNECT tunnels and record only bounded, non-secret counts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[bool] = []
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(32)
        self._server = server
        self.port = int(server.getsockname()[1])
        self._thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name="v012-connect-forwarder",
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def summary(self) -> ForwarderSummary:
        with self._lock:
            records = list(self._records)
        loopback = sum(records)
        return ForwarderSummary(
            total_connects=len(records),
            external_connects=len(records) - loopback,
            loopback_connects=loopback,
        )

    def _accept_loop(self) -> None:
        while True:
            server = self._server
            if server is None:
                return
            try:
                client, _address = server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve,
                args=(client,),
                daemon=True,
                name="v012-connect-client",
            ).start()

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(30)
            head = bytearray()
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head.extend(chunk)
                if len(head) > 64 * 1024:
                    return
            request_line = bytes(head).split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, separator, raw_port = parts[1].rpartition(":")
            if not separator or not raw_port.isdecimal():
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            port = int(raw_port)
            is_loopback = host.lower() in {"127.0.0.1", "localhost", "::1", "[::1]"}
            with self._lock:
                self._records.append(is_loopback)

            upstream = socket.create_connection((host, port), timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = [client, upstream]
            while True:
                readable, _writable, _exceptional = select.select(sockets, [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(65_536)
                    if not data:
                        return
                    target = upstream if source is client else client
                    target.sendall(data)
        except (OSError, ValueError):
            return
        finally:
            for stream in (client, upstream):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass
