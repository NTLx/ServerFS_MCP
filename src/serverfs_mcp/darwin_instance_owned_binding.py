"""macOS v0.25 owned Core -> Bridge startup identity and policy gate.

No launchd action. A named Core may only address its *own* independently
derived Bridge when its private manifest, TOML digest, Bridge policy and
environment all agree. Default macOS and all Windows wiring is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from . import darwin_service
from .darwin_instance_bridge_identity import named_owned_bridge_resources
from .darwin_instance_owned_staging import check_owned_bridge_policy
from .darwin_instance_runtime import (
    DarwinInstanceRuntimeError,
    _check_dir,
    _check_path,
    _check_private_file,
    _service_supported,
)
from .darwin_instances import instance_service_home
from .instance_identity import InstanceIdentity
from .native_config import load_native_config

OWNER = "v0.25-named-owned-core"
INSTANCE_ENV = "SERVERFS_MACOS_INSTANCE_ID"


class DarwinOwnedBindingError(ValueError):
    """The named Core's independent Bridge identity cannot be proven."""


def _owned_service_supported(key: str) -> bool:
    if key == "agent-bridge":
        return True
    try:
        return _service_supported(key)
    except DarwinInstanceRuntimeError:
        return False


def resolve_owned_core_binding(
    identity: InstanceIdentity,
    config_path: Path,
    port: int,
    *,
    home: Path | None = None,
    default_bridge_config: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, Path]:
    """Verify a named owned Core and return its immutable socket/lease identity.

    The instance ID comes from an explicit Core serve argument, not a stray
    ambient environment variable. Call this before any MCP/HTTP tool
    registration. The Bridge itself is never started by this function.
    """
    if identity.instance_id == "default" or identity.bridge_mode != "owned":
        raise DarwinOwnedBindingError("named owned Bridge identity required")
    if identity.bridge_id != identity.instance_id:
        raise DarwinOwnedBindingError("owned Bridge identity must equal Core identity")
    if type(port) is not int or not 1 <= port <= 65535:
        raise DarwinOwnedBindingError("owned Core HTTP port is invalid")
    root = instance_service_home(identity, home=home)
    try:
        _check_dir(root, private=True)
        manifest_path = root / "services.json"
        _check_private_file(manifest_path)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinOwnedBindingError("owned Core private manifest cannot be verified") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("owner") != OWNER
        or document.get("instance_id") != identity.instance_id
        or document.get("bridge_mode") != "owned"
        or document.get("bridge_id") != identity.instance_id
        or not isinstance(document.get("services"), dict)
        or not {"http-origin", "agent-bridge"} <= set(document["services"])
        or any(not _owned_service_supported(key) for key in document["services"])
        or not isinstance(document.get("core"), dict)
        or document["core"].get("port") != port
    ):
        raise DarwinOwnedBindingError("owned Core private manifest identity mismatch")

    raw_config = Path(config_path).expanduser()
    try:
        _check_path(raw_config, kind="owned Core TOML")
        resolved_config = raw_config.resolve(strict=True)
        config_bytes = resolved_config.read_bytes()
        workdirs, settings = load_native_config(resolved_config)
    except (OSError, ValueError) as exc:
        raise DarwinOwnedBindingError("owned Core TOML cannot be verified") from exc
    if not settings.bridge_required or not workdirs:
        raise DarwinOwnedBindingError("owned Core must enable Agent or Repo Ops in TOML")
    core = document["core"]
    if (
        core.get("config") != str(resolved_config)
        or core.get("sha256") != hashlib.sha256(config_bytes).hexdigest()
    ):
        raise DarwinOwnedBindingError("owned Core TOML digest or location drift")

    services = document["services"]
    core_spec = services["http-origin"]
    bridge_spec = services["agent-bridge"]
    if not isinstance(core_spec, dict) or not isinstance(bridge_spec, dict):
        raise DarwinOwnedBindingError("owned Core/Bridge service specs are invalid")
    expected_core_args = [
        "-m",
        "serverfs_mcp.cli",
        "serve",
        "--transport",
        "streamable-http",
        "--config",
        str(resolved_config),
        "--http-port",
        str(port),
        "--macos-instance",
        identity.instance_id,
    ]
    if (
        core_spec.get("arguments") != expected_core_args
        or core_spec.get("environment_file") != str(root / "http-origin-environment.json")
        or core_spec.get("origin_owner") != "independent-core"
    ):
        raise DarwinOwnedBindingError("owned Core launch arguments are untrusted")

    resources = named_owned_bridge_resources(identity, home=home)
    bridge_python_raw = bridge_spec.get("bridge_python")
    if not isinstance(bridge_python_raw, str) or not bridge_python_raw:
        raise DarwinOwnedBindingError("owned Bridge executable identity is invalid")
    bridge_python = Path(bridge_python_raw)
    if (
        not bridge_python.is_absolute()
        or not bridge_python.is_file()
        or not os.access(bridge_python, os.X_OK)
        or bridge_spec.get("executable") != core_spec.get("executable")
        or bridge_spec.get("arguments")
        != [
            "-B",
            "-m",
            "serverfs_mcp.darwin_owned_bridge_supervisor",
            "--instance",
            identity.instance_id,
        ]
        or bridge_spec.get("bridge_config") != str(resources.config_path)
        or bridge_spec.get("environment_file") is not None
        or bridge_spec.get("origin_owner") != "independent-bridge"
    ):
        raise DarwinOwnedBindingError("owned Bridge launch arguments are untrusted")
    try:
        _check_private_file(root / "http-origin-environment.json")
        snapshot = json.loads((root / "http-origin-environment.json").read_text())
    except (OSError, ValueError, UnicodeError) as exc:
        raise DarwinOwnedBindingError("owned Core private environment is invalid") from exc
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("SERVERFS_DATA_HOME") != str(root / "data")
        or snapshot.get("SERVERFS_FILE_INGRESS_ENABLED") != "false"
    ):
        raise DarwinOwnedBindingError("owned Core private environment is not isolated")

    env = os.environ if environ is None else environ
    if env.get(INSTANCE_ENV, "") not in ("", identity.instance_id):
        raise DarwinOwnedBindingError("named Core environment identity disagrees")
    if env.get("SERVERFS_DATA_HOME", str(root / "data")) != str(root / "data"):
        raise DarwinOwnedBindingError("owned Core data home does not match manifest")
    if env.get("SERVERFS_FILE_INGRESS_ENABLED", "false") != "false":
        raise DarwinOwnedBindingError("owned Core must disable shared file ingress")
    expected_socket = str(resources.socket_path)
    if env.get("SERVERFS_AGENT_BRIDGE_SOCKET", expected_socket) != expected_socket:
        raise DarwinOwnedBindingError("owned Core socket injection disagrees")
    if env.get("SERVERFS_AGENT_LOCK_DIR", str(resources.lock_dir)) != str(resources.lock_dir):
        raise DarwinOwnedBindingError("owned Core lock directory injection disagrees")

    # Optional Connector entries are not arbitrary executable permissions:
    # validate their pinned argv, private credentials and Core port binding.
    from .darwin_instance_runtime import (
        DarwinInstanceRuntimeError,
        _verify_cloudflare_spec,
        _verify_openai_spec,
    )

    try:
        _verify_cloudflare_spec(root, document, port)
        _verify_openai_spec(root, document, port)
    except (
        DarwinInstanceRuntimeError,
        darwin_service.DarwinServiceError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        raise DarwinOwnedBindingError(
            "owned Connector arguments or credentials are invalid"
        ) from exc

    try:
        check_owned_bridge_policy(
            identity,
            resolved_config,
            bridge_python,
            home=home,
            default_bridge_config=default_bridge_config,
        )
    except (OSError, ValueError) as exc:
        raise DarwinOwnedBindingError("owned Bridge policy or writer grants are stale") from exc
    return expected_socket, resources.lock_dir
