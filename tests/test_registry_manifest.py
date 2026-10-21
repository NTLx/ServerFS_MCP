import copy
import importlib.util
import json
from pathlib import Path

import pytest

_VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "deployment/registry/validate.py"
_VALIDATOR_SPEC = importlib.util.spec_from_file_location("registry_validator", _VALIDATOR_PATH)
assert _VALIDATOR_SPEC is not None and _VALIDATOR_SPEC.loader is not None
registry_validator = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(registry_validator)
MANIFEST_PATH = registry_validator.MANIFEST_PATH
validate_manifest = registry_validator.validate_manifest


@pytest.fixture
def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_registry_manifest_is_valid():
    validate_manifest()


def test_dockerfile_bootstraps_read_only_disabled_slots():
    dockerfile = (MANIFEST_PATH.parents[0] / "Dockerfile").read_text(encoding="utf-8")
    assert "for slot in $(seq -w 1 16)" in dockerfile
    assert 'touch "/workdirs/${slot}/.serverfs-disabled"' in dockerfile
    assert 'chown -R 10001:10001 "/workdirs/${slot}"' in dockerfile
    assert 'chmod 0555 "/workdirs/${slot}"' in dockerfile
    assert 'chmod 0444 "/workdirs/${slot}/.serverfs-disabled"' in dockerfile


def test_registry_manifest_does_not_declare_unused_workdir_path(manifest):
    environment = manifest["packages"][0]["environmentVariables"]
    assert "WORKDIR_01_PATH" not in {item["name"] for item in environment}

    changed = copy.deepcopy(manifest)
    changed["packages"][0]["environmentVariables"].append(
        {"name": "WORKDIR_01_PATH", "value": "/workdirs/01"}
    )
    with pytest.raises(ValueError, match="WORKDIR_01_PATH is unused"):
        validate_manifest(changed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("version",), "0.19.0"),
        (("packages", 0, "identifier"), "ghcr.io/ntlx/serverfs_mcp:latest"),
        (("packages", 0, "transport", "url"), "http://localhost:8000/mcp"),
        (("repository", "id"), "other"),
    ],
)
def test_registry_project_invariants_reject_drift(manifest, path, value):
    changed = copy.deepcopy(manifest)
    target = changed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_manifest(changed)


def test_registry_schema_rejects_invalid_description(manifest):
    changed = copy.deepcopy(manifest)
    changed["description"] = ""
    with pytest.raises(ValueError, match="schema validation"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_secret(manifest):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["environmentVariables"].append(
        {"name": "SERVERFS_API_KEY", "value": "test-secret"}
    )
    with pytest.raises(ValueError, match="must not contain secrets"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_host_path(manifest):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["runtimeArguments"][-1]["value"] = (
        "type=bind,src=/home/maintainer/workspace,dst=/workdirs/01,readonly"
    )
    with pytest.raises(ValueError, match="host-specific absolute path"):
        validate_manifest(changed)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda mount: mount.update(value=mount["value"].removesuffix(",readonly")), "--mount"),
        (
            lambda mount: mount.update(
                value=mount["value"].replace("dst=/workdirs/01", "dst=/tmp")
            ),
            "--mount",
        ),
        (lambda mount: mount.update(value="type=bind,src={workspace_path}"), "--mount"),
        (lambda mount: mount.update(variables={}), "--mount"),
        (
            lambda mount: mount["variables"]["workspace_path"].update(isRequired=False),
            "workspace_path",
        ),
        (
            lambda mount: mount["variables"]["workspace_path"].update(format="string"),
            "workspace_path",
        ),
        (
            lambda mount: mount["variables"]["workspace_path"].update(isSecret=True),
            "must not contain secrets",
        ),
    ],
)
def test_registry_runtime_configuration_rejects_unsafe_mount(manifest, change, message):
    changed = copy.deepcopy(manifest)
    mount = next(
        item for item in changed["packages"][0]["runtimeArguments"] if item.get("name") == "--mount"
    )
    change(mount)
    with pytest.raises(ValueError, match=message):
        validate_manifest(changed)


@pytest.mark.parametrize(
    "extra_argument",
    [
        {"type": "named", "name": "--mount", "value": "src=/other,dst=/workdirs/01"},
        {"type": "named", "name": "--rm", "value": "false"},
    ],
)
def test_registry_runtime_configuration_rejects_repeated_critical_arguments(
    manifest, extra_argument
):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["runtimeArguments"].append(extra_argument)
    with pytest.raises(ValueError, match="runtime argument"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_split_mount(manifest):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["runtimeArguments"].append(
        {"type": "named", "name": "--mount", "value": "readonly"}
    )
    with pytest.raises(ValueError, match="--mount"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_duplicate_environment(manifest):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["environmentVariables"].append(
        {"name": "WORKDIR_01_READ_ONLY", "value": "false"}
    )
    with pytest.raises(ValueError, match="must not be repeated"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_sensitive_environment_name(manifest):
    changed = copy.deepcopy(manifest)
    changed["packages"][0]["environmentVariables"].append(
        {"name": "SERVERFS_ACCESS_TOKEN", "value": "placeholder"}
    )
    with pytest.raises(ValueError, match="must not contain secrets"):
        validate_manifest(changed)


def test_registry_runtime_configuration_rejects_host_path_input_default(manifest):
    changed = copy.deepcopy(manifest)
    mount = changed["packages"][0]["runtimeArguments"][-1]
    mount["variables"]["workspace_path"]["default"] = "/Users/example/workspace"
    with pytest.raises(ValueError, match="host-specific absolute path"):
        validate_manifest(changed)


def test_registry_runtime_configuration_allows_token_in_description_and_name(manifest):
    changed = copy.deepcopy(manifest)
    changed["description"] += " Token-aware workspace access."
    changed["packages"][0]["runtimeArguments"][-1]["variables"]["workspace_path"]["description"] = (
        "Workspace path token used by the mount."
    )
    validate_manifest(changed)


def test_registry_name_must_match_docker_ownership_label(manifest, monkeypatch):
    monkeypatch.setattr(registry_validator, "_docker_label", lambda: "other/server")
    with pytest.raises(ValueError, match="Docker OCI ownership label"):
        validate_manifest(manifest)
