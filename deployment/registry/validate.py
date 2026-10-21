"""Offline validation for the MCP Registry package metadata."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "deployment/registry/2025-12-11.json"
MANIFEST_PATH = ROOT / "server.json"
OCI_NAME = "io.github.NTLx/serverfs-mcp"
OCI_LABEL = "io.modelcontextprotocol.server.name"
OCI_REPOSITORY = "ghcr.io/ntlx/serverfs_mcp"
HTTP_URL = "http://127.0.0.1:8000/mcp"
REPOSITORY_ID = "1375202485"
MOUNT_VALUE = "type=bind,src={workspace_path},dst=/workdirs/01,readonly"
SENSITIVE_ENV_PART = re.compile(r"(?:^|_)(?:password|passwd|token|secret|api_key|key)(?:_|$)", re.I)
HOST_PATH = re.compile(
    r"(?:^|[=,\s\"'])/(?:home|Users|tmp|var|opt|mnt)(?:/|$)|(?:^|[=,\s\"'])[A-Za-z]:[\\/]"
)


def _read_project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]["version"]


def validate_manifest(manifest: dict[str, Any] | None = None) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    data = manifest or json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(data),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"server.json schema validation failed: {details}")

    version = _read_project_version()
    package = _single_package(data)
    if data["version"] != version or package.get("version") != version:
        raise ValueError("server.json and package versions must match root project version")
    if package.get("identifier") != f"{OCI_REPOSITORY}:{version}":
        raise ValueError("OCI identifier must use the exact root project version tag")
    if re.search(r":(?:latest|edge)$", package["identifier"]):
        raise ValueError("mutable OCI tags are forbidden")
    if data["name"] != OCI_NAME or _docker_label() != OCI_NAME:
        raise ValueError("server name must match the Docker OCI ownership label")
    if data.get("repository", {}).get("id") != REPOSITORY_ID:
        raise ValueError("repository.id does not match the confirmed GitHub repository ID")
    if package.get("transport") != {"type": "streamable-http", "url": HTTP_URL}:
        raise ValueError("package transport must use the declared loopback Streamable HTTP URL")
    _validate_runtime_configuration(package)


def _single_package(data: dict[str, Any]) -> dict[str, Any]:
    packages = data.get("packages", [])
    if len(packages) != 1:
        raise ValueError("server.json must declare exactly one OCI package")
    return packages[0]


def _docker_label() -> str | None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(rf'^LABEL\s+{re.escape(OCI_LABEL)}="([^"]+)"\s*$', dockerfile, re.MULTILINE)
    return match.group(1) if match else None


def _validate_runtime_configuration(package: dict[str, Any]) -> None:
    if package.get("runtimeHint") != "docker":
        raise ValueError("OCI runtimeHint must be docker")
    arguments = package.get("runtimeArguments", [])
    for item in arguments:
        _validate_configurable_input(item)
        for variable in item.get("variables", {}).values():
            _validate_configurable_input(variable)
    positional = [item for item in arguments if item.get("type") == "positional"]
    named = [item for item in arguments if item.get("type") == "named"]
    if len(positional) != 1 or positional[0].get("value") != "run":
        raise ValueError("Docker runtime arguments must contain the single positional run")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in named:
        by_name.setdefault(item.get("name"), []).append(item)
    expected_named = {
        "--rm": "true",
        "--publish": "127.0.0.1:8000:8000",
        "--mount": MOUNT_VALUE,
    }
    for name, value in expected_named.items():
        matches = by_name.get(name, [])
        if len(matches) != 1 or matches[0].get("value") != value:
            raise ValueError(f"Docker runtime argument {name} must have its exact required value")
    mount_variables = by_name["--mount"][0].get("variables", {})
    if set(mount_variables) != {"workspace_path"}:
        raise ValueError("--mount variables must contain only workspace_path")
    workspace = mount_variables["workspace_path"]
    _validate_configurable_input(workspace)
    if (
        workspace.get("format") != "filepath"
        or workspace.get("isRequired") is not True
        or workspace.get("isSecret") is not False
    ):
        raise ValueError("workspace_path must be a required, non-secret filepath")

    environment_items = package.get("environmentVariables", [])
    environment: dict[str, Any] = {}
    for item in environment_items:
        name = item["name"]
        if name in environment:
            raise ValueError(f"runtime environment variable {name} must not be repeated")
        environment[name] = item.get("value", "")
        if SENSITIVE_ENV_PART.search(name):
            raise ValueError("runtime configuration must not contain secrets")
        _validate_configurable_input(item)
    expected = {
        "WORKDIR_01_ALIAS": "workspace",
        "WORKDIR_01_READ_ONLY": "true",
        "SERVERFS_HTTP_ALLOWED_HOSTS": "127.0.0.1:8000,localhost:8000",
        "SERVERFS_HTTP_ALLOWED_ORIGINS": "http://127.0.0.1:8000,http://localhost:8000",
    }
    if any(environment.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "runtime environment must use the workdir alias, read-only and HTTP Host/Origin values"
        )
    if "WORKDIR_01_PATH" in environment:
        raise ValueError("WORKDIR_01_PATH is unused; the workspace path is selected by bind mount")


def _validate_configurable_input(item: dict[str, Any]) -> None:
    if item.get("isSecret") is True:
        raise ValueError("runtime configuration must not contain secrets")
    for key in ("value", "default", "placeholder"):
        value = item.get(key)
        if isinstance(value, str) and HOST_PATH.search(value):
            raise ValueError("runtime configuration must not contain a host-specific absolute path")


def main() -> int:
    validate_manifest()
    print("server.json schema and project invariants are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
