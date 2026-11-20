"""Pure I3 shared-Bridge Core grant issuance contracts; no file or host I/O."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.windows_v25.core_bridge_binding import parse_core_bridge_binding
from serverfs_mcp.windows_v25.shared_core_grants import (
    SharedGrantIssueError,
    issue_shared_core_capabilities,
)
from serverfs_mcp.windows_v25.spec import Manifest

SID = "S-1-5-21-111-222-333-1001"
DIGEST = "a" * 64
METHODS = ("bridge.ping", "runtime.list", "task.submit", "task.get")


def _issue(
    *, bridge_id="i3-shared", core_ids=("i3-core-a", "i3-core-b"), workdirs=None, methods=METHODS
):
    return issue_shared_core_capabilities(
        bridge_id=bridge_id,
        core_ids=core_ids,
        workdir_policy_revisions={"Fixture": DIGEST} if workdirs is None else workdirs,
        methods=methods,
    )


def _manifest(tmp_path: Path, *, component: str, instance_id: str):
    settings = (
        {
            "config": str(tmp_path / instance_id / "bridge.json"),
            "bridge_python": str(tmp_path / instance_id / "agent-bridge" / "python.exe"),
        }
        if component == "bridge"
        else {
            "config": str(tmp_path / instance_id / "core.toml"),
            "auth_file": str(tmp_path / instance_id / "http.env"),
            "port": 18869 if instance_id == "i3-core-a" else 18870,
            "hostname": f"{instance_id}.example.test",
        }
    )
    return Manifest.parse(
        {
            "schema": 2,
            "component": component,
            "instance_id": instance_id,
            "sid": SID,
            "pythonw": str(tmp_path / instance_id / "pythonw.exe"),
            "settings": settings,
        }
    )


def test_two_independent_core_credentials_and_bridge_records(tmp_path: Path) -> None:
    issued = _issue()
    assert len(issued) == 2
    assert {grant.core_id for grant in issued} == {"i3-core-a", "i3-core-b"}
    assert len({grant.key_id for grant in issued}) == 2
    assert len({grant.secret for grant in issued}) == 2
    peer = _manifest(tmp_path, component="bridge", instance_id="i3-shared")
    for grant in issued:
        assert grant.secret.hex() not in repr(grant)
        binding = grant.core_binding()
        bridge_record = grant.bridge_grant()
        assert binding["secret_hex"] == bridge_record["secret_hex"]
        assert binding["key_id"] == bridge_record["key_id"]
        assert binding["workdirs"] == bridge_record["workdirs"]
        assert bridge_record["methods"] == list(METHODS)
        assert "socket_path" not in binding
        assert "job_name" not in binding
        assert "host_path" not in binding
        assert "python" not in binding
        verified = parse_core_bridge_binding(
            _manifest(tmp_path, component="core", instance_id=grant.core_id),
            binding,
            local_aliases=frozenset({"Fixture"}),
            verified_bridge=peer,
            bridge_lock_dir=tmp_path / "locks",
        )
        assert verified.bridge_mode == "shared"
        assert verified.bridge_id == "i3-shared"
        assert verified.secret == grant.secret


@pytest.mark.parametrize(
    "overrides",
    [
        {"bridge_id": "default"},
        {"bridge_id": "../foreign"},
        {"core_ids": ()},
        {"core_ids": ("i3-core-a", "i3-core-a")},
        {"core_ids": ("default", "i3-core-b")},
        {"core_ids": ("i3-shared", "i3-core-b")},
        {"core_ids": ("../other", "i3-core-b")},
        {"core_ids": ("i3-core-a",) * 33},
        {"workdirs": {}},
        {"workdirs": {"Fixture": "wrong"}},
        {"workdirs": {"../foreign": DIGEST}},
        {"methods": ()},
        {"methods": ("bridge.ping", "bridge.ping")},
        {"methods": ("runtime.list",)},
        {"methods": ("bridge.ping", "unsupported.run")},
    ],
)
def test_invalid_identity_scope_or_capability_denied(overrides) -> None:
    with pytest.raises(SharedGrantIssueError, match="issuance refused"):
        _issue(**overrides)


def test_random_failure_or_key_collision_must_fail_entire_bundle(monkeypatch) -> None:
    from serverfs_mcp.windows_v25 import shared_core_grants as issuer

    monkeypatch.setattr(issuer.secrets, "token_hex", lambda _n: "0" * 16)
    monkeypatch.setattr(issuer.secrets, "token_bytes", lambda _n: b"Q" * 32)
    with pytest.raises(SharedGrantIssueError):
        _issue()


def test_grant_issuer_does_not_install_or_log_secrets() -> None:
    from serverfs_mcp.windows_v25 import shared_core_grants as issuer

    source = Path(issuer.__file__).read_text("utf-8")
    assert "TaskScheduler" not in source
    assert "subprocess" not in source
    assert "storage.stage" not in source
    assert "open(" not in source
    assert "print(" not in source
    assert "os.environ" not in source
