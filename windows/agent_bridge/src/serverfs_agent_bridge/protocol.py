"""Local Windows protocol entry point; no alternate POSIX implementation."""

from .protocol_windows_native import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PIPE_IDLE_TIMEOUT_SECONDS,
    PIPE_POOL_SIZE,
    PROTOCOL_VERSION,
    BridgeProtocolServer,
)

__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "PIPE_IDLE_TIMEOUT_SECONDS",
    "PIPE_POOL_SIZE",
    "PROTOCOL_VERSION",
    "BridgeProtocolServer",
]
