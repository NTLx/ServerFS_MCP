"""Synchronize derived native policy into an existing private Bridge config.

macOS v0.13 deliberately keeps provider/Jev network material in the Bridge's private 0600 state,
while ``serverfs.toml`` is the single operator-facing source for workdir/runtime/lifecycle policy.
This module reconciles those two ownership domains without copying secrets back through ServerFS:

- stdin contains only the non-secret render request derived from ``serverfs.toml``;
- the existing private config is verified and fully validated before it is read;
- only fields owned by the render request are replaced;
- private ``proxy`` / ``jev`` material and native endpoint/identity fields are preserved verbatim;
- the merged document is validated as a real ``BridgeConfig`` before atomic publication.

It is intentionally a Bridge-owned CLI so private-state publication continues to use the Bridge's
existing authorization and atomic-write primitives rather than duplicating them in ``serverfs_mcp``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .errors import BridgeError
from .private_state import verify_private_file
from .render_config import (
    _RUNTIME_KEYS,
    MAX_INPUT_BYTES,
    _publish_private_file,
    build_policy_document,
)

_DERIVED_KEYS = frozenset(
    {
        "lease_key",
        "enable_fake_runtime",
        "workdirs",
        "limits",
        "codex",
        "claude",
        "qoder",
    }
)
_PRIVATE_KEYS = frozenset({"proxy", "jev"})
_RUNTIME_NAMES = ("codex", "claude", "qoder")
_MAX_CONFIG_BYTES = 256 * 1024


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BridgeError("BRIDGE_CONFIG_INVALID", "the private Bridge config does not exist")
    verify_private_file(
        path,
        not_regular="the existing Bridge config is not a regular file",
        not_private="the existing Bridge config is not private",
    )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BridgeError(
            "BRIDGE_CONFIG_INVALID", "the private Bridge config cannot be inspected"
        ) from exc
    if size > _MAX_CONFIG_BYTES:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "the private Bridge config is too large")
    # Validate the current document before preserving anything from it.  In particular, this means
    # a malformed or unsafe proxy/Jev block is never carried forward merely because it is private.
    try:
        BridgeConfig.load(path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BridgeError("BRIDGE_CONFIG_INVALID", "the private Bridge config is invalid") from exc
    if not isinstance(data, dict):
        raise BridgeError("BRIDGE_CONFIG_INVALID", "the private Bridge config root is invalid")
    return data


def _workdirs_match(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    keys = ("alias", "host_path", "read_only", "agent_mode", "agent_runtimes")
    try:
        actual_projection = sorted(
            ({key: item.get(key) for key in keys} for item in actual if isinstance(item, dict)),
            key=lambda item: item["alias"],
        )
        expected_projection = sorted(
            ({key: item.get(key) for key in keys} for item in expected if isinstance(item, dict)),
            key=lambda item: item["alias"],
        )
    except (KeyError, TypeError):
        return False
    return len(actual_projection) == len(actual) and actual_projection == expected_projection


def _policy_matches(existing: dict[str, Any], request: dict[str, Any]) -> bool:
    """Whether every serverfs.toml-owned field already equals the derived policy."""
    derived = build_policy_document(request)
    if existing.get("lease_key") != derived.get("lease_key"):
        return False
    if bool(existing.get("enable_fake_runtime", False)) != bool(
        derived.get("enable_fake_runtime", False)
    ):
        return False
    if not _workdirs_match(existing.get("workdirs"), derived.get("workdirs")):
        return False

    expected_limits = derived.get("limits", {})
    actual_limits = existing.get("limits", {})
    if not isinstance(actual_limits, dict) or any(
        actual_limits.get(key) != value for key, value in expected_limits.items()
    ):
        return False

    for runtime in _RUNTIME_NAMES:
        expected_runtime = derived.get(runtime)
        actual_runtime = existing.get(runtime)
        if expected_runtime is None:
            # An explicitly disabled legacy block is behaviorally equivalent to absence; an enabled
            # stale block is not, because it could expose a provider no longer allowed by TOML.
            if isinstance(actual_runtime, dict) and actual_runtime.get("enabled", False):
                return False
            continue
        if not isinstance(actual_runtime, dict) or any(
            actual_runtime.get(key) != value for key, value in expected_runtime.items()
        ):
            return False
    return True


def _merge_derived_policy(existing: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    """Replace operator-owned policy while preserving runtime-local settings.

    The render request owns only the runtime keys accepted by ``_RUNTIME_KEYS``
    (for example ``enabled``, ``codex_bin`` and ``use_proxy``). Bridge-local
    settings such as ``codex_home`` and timeout/message limits belong to the
    private runtime configuration and must survive a policy synchronization.
    """
    merged = {key: value for key, value in existing.items() if key not in _DERIVED_KEYS}
    for key, value in derived.items():
        if key not in _RUNTIME_NAMES:
            merged[key] = value

    for runtime in _RUNTIME_NAMES:
        derived_runtime = derived.get(runtime)
        if derived_runtime is None:
            continue
        existing_runtime = existing.get(runtime)
        preserved_runtime = (
            {
                key: value
                for key, value in existing_runtime.items()
                if key not in _RUNTIME_KEYS[runtime]
            }
            if isinstance(existing_runtime, dict)
            else {}
        )
        merged[runtime] = {**preserved_runtime, **derived_runtime}
    return merged


def _merged_document(existing: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return _merge_derived_policy(existing, build_policy_document(request))


def check_bridge_config(config_path: Path, request: dict[str, Any]) -> bool:
    """Check derived policy without returning any private config content to the caller."""
    return _policy_matches(_load_existing(config_path), request)


def _validate_merged(config_path: Path, encoded: str) -> None:
    """Validate the exact candidate bytes before replacing the live private config."""
    candidate = config_path.parent / f".bridge.sync.{secrets.token_hex(8)}.json"
    try:
        _publish_private_file(candidate, encoded)
        BridgeConfig.load(candidate)
    except (OSError, ValueError) as exc:
        raise BridgeError(
            "BRIDGE_CONFIG_INVALID", "the synchronized Bridge config is invalid"
        ) from exc
    finally:
        candidate.unlink(missing_ok=True)


def sync_bridge_config(config_path: Path, request: dict[str, Any]) -> None:
    """Replace only TOML-derived policy in ``config_path`` and preserve private material."""
    existing = _load_existing(config_path)
    merged = _merged_document(existing, request)
    encoded = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    _validate_merged(config_path, encoded)
    _publish_private_file(config_path, encoded)


def configure_bridge_config(
    config_path: Path,
    request: dict[str, Any],
    private_overlay: dict[str, Any],
) -> None:
    """Create/update the private config from derived policy plus explicit private material.

    ``private_overlay`` is intentionally tiny: only ``proxy`` and ``jev`` are caller-owned private
    inputs. Endpoint, state and peer-identity fields are either preserved from an existing valid
    config or left to ``BridgeConfig``'s platform defaults on first creation.
    """
    unknown = set(private_overlay) - _PRIVATE_KEYS
    if unknown:
        raise BridgeError(
            "BRIDGE_CONFIG_INVALID",
            "the private Bridge overlay contains unsupported fields",
        )
    existing = _load_existing(config_path) if config_path.exists() else {}
    derived = build_policy_document(request)
    without_private = {key: value for key, value in existing.items() if key not in _PRIVATE_KEYS}
    candidate = {
        **_merge_derived_policy(without_private, derived),
        **private_overlay,
    }
    encoded = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
    _validate_merged(config_path, encoded)
    _publish_private_file(config_path, encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="serverfs-agent-bridge-sync-config",
        description="Synchronize non-secret native policy into an existing private Bridge config",
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="private Bridge JSON config path"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether derived policy matches without modifying the private config",
    )
    mode.add_argument(
        "--configure",
        action="store_true",
        help="create/update derived policy and explicit private proxy/Jev material",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        print("sync request exceeds the bounded input size", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("sync request must be a JSON object")
        config_path = args.config.expanduser().resolve()
        if args.configure:
            request = payload.get("policy")
            private_overlay = payload.get("private", {})
            if not isinstance(request, dict) or not isinstance(private_overlay, dict):
                raise ValueError("configure request must contain policy/private objects")
            configure_bridge_config(config_path, request, private_overlay)
            synced = True
        else:
            request = payload
            if args.check:
                synced = check_bridge_config(config_path, request)
            else:
                sync_bridge_config(config_path, request)
                synced = True
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"sync request is invalid: {exc}", file=sys.stderr)
        return 2
    except BridgeError as exc:
        print(f"bridge configuration sync refused: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"config_path": str(config_path), "synced": synced}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
