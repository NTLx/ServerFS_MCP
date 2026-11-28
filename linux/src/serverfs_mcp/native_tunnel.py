"""Linux proxy parsing for read-only diagnostics and tunnel configuration.

The actual Connector lifecycle is Docker-owned; there is no native launcher.
"""

from __future__ import annotations

from pathlib import Path

from .shared_proxy import PROXY_KEYS, parse_shared_proxy


class NativeTunnelError(ValueError):
    """Redacted, operator-safe tunnel configuration error."""


def parse_proxy_env_file(path: Path | None) -> dict[str, str]:
    """Read only the four shared proxy values, without expansion or evaluation."""
    if path is None:
        return {}
    if not path.exists():
        raise NativeTunnelError("proxy env file does not exist")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise NativeTunnelError("cannot read proxy env file") from exc

    for line_number, line in enumerate(lines, 1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        key, sep, raw = candidate.partition("=")
        key = key.strip()
        if key not in PROXY_KEYS:
            continue
        if key in values:
            raise NativeTunnelError(f"duplicate {key} at line {line_number}")
        if not sep:
            raise NativeTunnelError(f"invalid {key} assignment at line {line_number}")
        values[key] = parse_env_value(raw.strip(), key, line_number)
    return values


def parse_env_value(raw: str, key: str, line_number: int) -> str:
    if not raw or raw[0] not in "\"'":
        return raw.strip()
    quote = raw[0]
    out: list[str] = []
    escaped = False
    index = 1
    while index < len(raw):
        char = raw[index]
        if quote == '"' and escaped:
            if char not in {'"', "\\"}:
                out.append("\\")
            out.append(char)
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif char == quote:
            tail = raw[index + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise NativeTunnelError(f"invalid quoted {key} at line {line_number}")
            return "".join(out)
        else:
            out.append(char)
        index += 1
    raise NativeTunnelError(f"unterminated quoted {key} at line {line_number}")


def proxy_url(values: dict[str, str]) -> str | None:
    """Validate shared fields and derive the canonical HTTP proxy URL."""
    parsed = parse_shared_proxy(values, error_type=NativeTunnelError)
    return None if parsed is None else parsed.url


def encode_tunnel_command_argv(argv: list[str]) -> str:
    """Encode argv for tunnel-client's parseCommandArgv, even on Windows.

    That parser treats each backslash as an escape except inside single
    quotes, where embedded single quotes cannot be represented. Double
    quotes with doubled backslashes and escaped double quotes round-trip.
    """
    if not argv or any(not arg for arg in argv):
        raise NativeTunnelError("tunnel command arguments must be non-empty")
    return " ".join('"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' for arg in argv)
