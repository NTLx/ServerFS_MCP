"""v0.25 macOS owned-Bridge resource identity source contracts, no startup."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from serverfs_mcp import darwin_instance_cli, darwin_instance_runtime
from serverfs_mcp.darwin_instance_bridge_identity import (
    DarwinBridgeIdentityError,
    named_owned_bridge_resources,
    preflight_named_owned_bridge,
    refuse_bridge_resource_collisions,
    refuse_writable_conflicts_with_private_bridges,
    refuse_writable_workdir_overlaps,
    workdir_claims_from_private_bridge_config,
)
from serverfs_mcp.instance_identity import InstanceIdentity
from serverfs_mcp.workdirs import Workdir


def _owned(identifier: str) -> InstanceIdentity:
    return InstanceIdentity(instance_id=identifier, bridge_mode="owned", bridge_id=identifier)


def test_two_owned_bridges_have_separate_ipc_state_lock_and_config(tmp_path: Path) -> None:
    # Planning-only: do not use pytest's long temporary socket pathname.
    run = Path("/private/tmp")
    a = named_owned_bridge_resources(_owned("one"), home=tmp_path, runtime_root=run)
    b = named_owned_bridge_resources(_owned("two"), home=tmp_path, runtime_root=run)
    assert a.bridge_id == "one"
    assert a.socket_path != b.socket_path
    assert a.state_dir != b.state_dir
    assert a.lock_dir != b.lock_dir
    assert a.config_path != b.config_path
    assert a.socket_path.name == "bridge.sock"
    assert a.config_path.name == "bridge.json"
    assert "instances/one/service-manager/data/agent-bridge" in str(a.config_path)
    refuse_bridge_resource_collisions(a, (b,))


@pytest.mark.parametrize(
    "identity",
    [
        InstanceIdentity(),
        InstanceIdentity(instance_id="files", bridge_mode="disabled"),
        InstanceIdentity(instance_id="tenant", bridge_mode="shared", bridge_id="other"),
        InstanceIdentity(instance_id="default", bridge_mode="owned", bridge_id="default"),
    ],
)
def test_default_file_only_or_shared_refuses_owned_resource_owner(
    tmp_path: Path, identity: InstanceIdentity
) -> None:
    with pytest.raises(DarwinBridgeIdentityError, match="owned"):
        named_owned_bridge_resources(identity, home=tmp_path, runtime_root=tmp_path)


def test_duplicate_bridge_resource_owner_refused(tmp_path: Path) -> None:
    a = named_owned_bridge_resources(
        _owned("same"), home=tmp_path, runtime_root=Path("/private/tmp")
    )
    with pytest.raises(DarwinBridgeIdentityError, match="collision"):
        refuse_bridge_resource_collisions(a, (a,))


def test_long_darwin_socket_path_fails_before_binding(tmp_path: Path) -> None:
    long = tmp_path / ("long-" + "a" * 80)
    with pytest.raises(DarwinBridgeIdentityError, match="sun_path"):
        named_owned_bridge_resources(_owned("staging"), home=tmp_path, runtime_root=long)


def test_named_v1_cannot_claim_the_default_legacy_bridge_socket(tmp_path: Path) -> None:
    run = Path("/private/tmp")
    planned = named_owned_bridge_resources(_owned("v1"), home=tmp_path, runtime_root=run)
    assert planned.socket_path != run / "serverfs-agent-bridge-v1/bridge.sock"


def _claim(alias: str, root: Path, *, writable: bool = True) -> Workdir:
    return Workdir(alias=alias, root=root, description=None, read_only=not writable)


def test_two_bridge_writer_claims_to_same_root_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    candidate = (_claim("one", root),)
    existing = ((_claim("other", root),),)
    with pytest.raises(DarwinBridgeIdentityError, match="independent Bridges"):
        refuse_writable_workdir_overlaps(candidate, existing)


def test_independent_bridges_reject_nested_writer_roots(tmp_path: Path) -> None:
    parent = tmp_path / "project"
    child = parent / "src"
    child.mkdir(parents=True)
    with pytest.raises(DarwinBridgeIdentityError, match="overlapping writable"):
        refuse_writable_workdir_overlaps((_claim("one", parent),), ((_claim("other", child),),))
    with pytest.raises(DarwinBridgeIdentityError, match="overlapping writable"):
        refuse_writable_workdir_overlaps((_claim("one", child),), ((_claim("other", parent),),))


def test_same_bridge_rejects_aliases_to_same_writable_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(DarwinBridgeIdentityError, match="same Bridge"):
        refuse_writable_workdir_overlaps((_claim("one", root), _claim("two", alias)), ())


def test_distinct_writer_roots_and_readonly_overlaps_are_allowed(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    # Read-only mappings never claim a writer lease in another Bridge.
    refuse_writable_workdir_overlaps(
        (_claim("write-one", one),),
        ((_claim("readonly", one, writable=False), _claim("write-two", two)),),
    )
    refuse_writable_workdir_overlaps(
        (_claim("readonly", one, writable=False),),
        ((_claim("write-one", one),),),
    )


def test_invalid_or_missing_writer_claim_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DarwinBridgeIdentityError, match="unavailable"):
        refuse_writable_workdir_overlaps((_claim("missing", tmp_path / "not-created"),), ())
    with pytest.raises(DarwinBridgeIdentityError, match="invalid type"):
        refuse_writable_workdir_overlaps(("arbitrary",), ())


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS OS runtime only")
def test_verified_macos_runtime_is_canonical_and_socket_fits() -> None:
    from serverfs_mcp.darwin_libc import darwin_runtime_dir

    resources = named_owned_bridge_resources(_owned("real-runtime"))
    canonical = darwin_runtime_dir().resolve(strict=True)
    assert resources.socket_path.is_relative_to(canonical)
    assert len(os.fsencode(str(resources.socket_path))) <= 103
    assert not resources.socket_path.parent.exists()


def _private_bridge_policy(path: Path, workdir: Path, *, read_only: bool = False) -> Path:
    path.write_text(
        json.dumps(
            {
                "workdirs": [
                    {
                        "alias": "another-bridge",
                        "host_path": str(workdir),
                        "read_only": read_only,
                    }
                ],
                # A secret-bearing field must never appear in a refusal message.
                "jev": {"api_key": "SENTINEL_PRIVATE_KEY"},
            }
        )
    )
    path.chmod(0o600)
    return path


def test_owned_bridge_refuses_default_private_writer_overlap(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    config = _private_bridge_policy(tmp_path / "bridge.json", root)
    claims = workdir_claims_from_private_bridge_config(config)
    assert len(claims) == 1 and not claims[0].read_only
    with pytest.raises(DarwinBridgeIdentityError, match="overlapping writable"):
        refuse_writable_conflicts_with_private_bridges((_claim("proposed", root),), (config,))


def test_owned_bridge_allows_independent_and_readonly_private_claims(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    other = tmp_path / "other"
    shared.mkdir()
    other.mkdir()
    config = _private_bridge_policy(tmp_path / "bridge.json", shared, read_only=True)
    refuse_writable_conflicts_with_private_bridges((_claim("proposed", shared),), (config,))
    _private_bridge_policy(config, other, read_only=False)
    refuse_writable_conflicts_with_private_bridges((_claim("proposed", shared),), (config,))


def test_owned_bridge_refuses_unsafe_private_policy_without_secret_leak(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    config = _private_bridge_policy(tmp_path / "bridge.json", root)
    config.chmod(0o644)
    with pytest.raises(DarwinBridgeIdentityError) as exc:
        workdir_claims_from_private_bridge_config(config)
    assert "SENTINEL_PRIVATE_KEY" not in str(exc.value)
    config.chmod(0o600)
    config.write_text('{"workdirs":[{"alias":"broken","host_path":"/tmp"}]}')
    with pytest.raises(DarwinBridgeIdentityError, match="invalid"):
        workdir_claims_from_private_bridge_config(config)
    config.write_text("invalid json")
    with pytest.raises(DarwinBridgeIdentityError, match="malformed"):
        workdir_claims_from_private_bridge_config(config)


def test_owned_bridge_refuses_symlinked_private_config(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    config = _private_bridge_policy(tmp_path / "bridge.json", root)
    alias = tmp_path / "alias.json"
    alias.symlink_to(config)
    with pytest.raises(DarwinBridgeIdentityError, match="unsafe"):
        workdir_claims_from_private_bridge_config(alias)


def _candidate_owned_toml(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "owned.toml"
    path.write_text(
        "[agent]\nenabled = true\n"
        "[agent.codex]\nenabled = true\n"
        '[[workdirs]]\nalias = "owned"\n'
        f'path = "{root}"\n'
        'read_only = false\nagent_mode = "workspace-write"\n'
        'agent_runtimes = ["codex"]\n'
    )
    return path


def test_owned_preflight_refuses_existing_default_bridge_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    candidate = _candidate_owned_toml(tmp_path, root)
    default = _private_bridge_policy(tmp_path / "default-bridge.json", root)
    with pytest.raises(DarwinBridgeIdentityError, match="overlapping writable"):
        preflight_named_owned_bridge(
            _owned("new"),
            candidate,
            home=tmp_path,
            default_bridge_config=default,
            runtime_root=Path("/private/tmp"),
        )


def test_owned_preflight_allows_disjoint_private_writers(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "new-project"
    default_root = tmp_path / "default-project"
    candidate_root.mkdir()
    default_root.mkdir()
    candidate = _candidate_owned_toml(tmp_path, candidate_root)
    default = _private_bridge_policy(tmp_path / "default-bridge.json", default_root)
    status = preflight_named_owned_bridge(
        _owned("new"),
        candidate,
        home=tmp_path,
        default_bridge_config=default,
        runtime_root=Path("/private/tmp"),
    )
    assert status == {
        "instance_id": "new",
        "bridge_mode": "owned",
        "workdirs_checked": 1,
        "peer_bridge_policies_checked": 1,
        "result": "PASS_SOURCE_PREFLIGHT_ONLY",
    }


def test_owned_preflight_rejects_unknown_existing_named_bridge_owner(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "new-project"
    default_root = tmp_path / "default-project"
    candidate_root.mkdir()
    default_root.mkdir()
    candidate = _candidate_owned_toml(tmp_path, candidate_root)
    default = _private_bridge_policy(tmp_path / "default-bridge.json", default_root)
    named = tmp_path / "Library/Application Support/ServerFS/instances/other"
    owned = named / "service-manager"
    owned.mkdir(parents=True, mode=0o700)
    named.chmod(0o700)
    owned.chmod(0o700)
    instances = named.parent
    instances.chmod(0o700)
    manifest = owned / "services.json"
    manifest.write_text(
        json.dumps(
            {
                "owner": "v0.25-named-file-core",
                "instance_id": "other",
                "bridge_mode": "shared",
                "bridge_id": "default",
                "core": {"port": 28098},
                "services": {"http-origin": {}},
            }
        )
    )
    manifest.chmod(0o600)
    with pytest.raises(DarwinBridgeIdentityError, match="peer ownership|ownership mode"):
        preflight_named_owned_bridge(
            _owned("new"),
            candidate,
            home=tmp_path,
            default_bridge_config=default,
            runtime_root=Path("/private/tmp"),
        )


def test_owned_preflight_requires_agent_or_repo_ops_policy(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    config = tmp_path / "files.toml"
    config.write_text(f'[[workdirs]]\nalias = "files"\npath = "{root}"\n')
    default = _private_bridge_policy(tmp_path / "default-bridge.json", root, read_only=True)
    with pytest.raises(DarwinBridgeIdentityError, match="requires"):
        preflight_named_owned_bridge(
            _owned("new"),
            config,
            home=tmp_path,
            default_bridge_config=default,
            runtime_root=Path("/private/tmp"),
        )


def test_owned_preflight_cli_does_not_touch_service_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import serverfs_mcp.darwin_instance_bridge_identity as bridge_identity

    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    observed = []

    def accept(identity, config):
        observed.append((identity.bridge_mode, identity.bridge_id, config))
        return {"instance_id": identity.instance_id, "result": "PASS_SOURCE_PREFLIGHT_ONLY"}

    monkeypatch.setattr(bridge_identity, "preflight_named_owned_bridge", accept)
    monkeypatch.setattr(
        darwin_instance_runtime,
        "instance_status",
        lambda *_args, **_kwargs: pytest.fail("CLI must not query installed native services"),
    )
    code = darwin_instance_cli.main(
        [
            "--instance",
            "owned-test",
            "owned-preflight",
            "--config",
            str(tmp_path / "owned.toml"),
        ]
    )
    assert code == 0
    assert observed == [("owned", "owned-test", tmp_path / "owned.toml")]
    assert json.loads(capsys.readouterr().out) == {
        "instance_id": "owned-test",
        "result": "PASS_SOURCE_PREFLIGHT_ONLY",
    }


def test_owned_preflight_cli_refuses_legacy_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(darwin_instance_cli.sys, "platform", "darwin")
    assert (
        darwin_instance_cli.main(
            [
                "--instance",
                "default",
                "owned-preflight",
                "--config",
                str(tmp_path / "owned.toml"),
            ]
        )
        == 2
    )
    assert "owned Bridge" in capsys.readouterr().err


def test_named_bridge_resource_planning_has_no_file_system_effect(tmp_path: Path) -> None:
    run = Path("/serverfs-v25-runtime-not-created-08c75f46")
    result = named_owned_bridge_resources(_owned("private"), home=tmp_path, runtime_root=run)
    suffix = hashlib.sha256(b"private").hexdigest()[:16]
    assert result.socket_path.parent == run / f"sfbr25-{suffix}"
    assert not run.exists()
    assert not result.state_dir.exists()
    assert not result.lock_dir.exists()
