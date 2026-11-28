"""Provider-neutral Agent Bridge errors."""

from __future__ import annotations


class BridgeError(Exception):
    """Expected error that can safely cross the Bridge RPC boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise BridgeError(code, message)
