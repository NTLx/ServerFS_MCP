"""Portable tests for named Windows initial private configuration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "private_provision",
    Path(__file__).resolve().parents[1] / "deployment/windows/configure-v25-private.py",
)
assert spec is not None and spec.loader is not None
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def core(identity="alpha", port="18890"):
    return (identity, port, "alpha.local", "policy.toml")


@pytest.mark.parametrize(
    ("cores", "bridge", "policy", "mode"),
    [
        ([core()], None, None, "disabled"),
        ([core()], "alpha", None, "owned"),
        ([core(), core("beta", "18891")], "owner", Path("owner.toml"), "shared"),
    ],
)
def test_topology(cores, bridge, policy, mode):
    assert tool.validate_topology(cores, bridge, policy) == mode


@pytest.mark.parametrize(
    ("cores", "bridge", "policy"),
    [
        ([core("default")], None, None),
        ([core(), core()], "owner", Path("owner.toml")),
        ([core(), core("beta")], "owner", Path("owner.toml")),
        ([core(), core("beta", "18891")], "alpha", None),
        ([core()], "owner", None),
        ([core()], None, Path("owner.toml")),
        ([core("alpha", "0")], None, None),
        ([core()], "default", Path("owner.toml")),
        ([core(), core("beta", "18891")], None, None),
    ],
)
def test_invalid_topology(cores, bridge, policy):
    with pytest.raises(ValueError):
        tool.validate_topology(cores, bridge, policy)


def test_http_auth_is_independent_and_exact():
    first = tool.http_auth("alpha.local", 18890).decode().splitlines()
    second = tool.http_auth("alpha.local", 18890).decode().splitlines()
    assert first[0].startswith("SERVERFS_HTTP_API_KEY=") and first[0] != second[0]
    assert (
        first[1:]
        == second[1:]
        == [
            "SERVERFS_HTTP_ALLOWED_HOSTS=alpha.local,127.0.0.1:18890",
            "SERVERFS_OAUTH_ENABLED=false",
        ]
    )
    with pytest.raises(ValueError):
        tool.http_auth("bad\nhost", 18890)


def workdir(alias="Repo", repo=False, runtimes=()):
    return {
        "alias": alias,
        "host_path": "C:/fixture",
        "read_only": not repo and not runtimes,
        "agent_mode": "workspace-write" if runtimes else "disabled",
        "agent_runtimes": list(runtimes),
        **({"repo_ops_enabled": True, "repo_actions": {}} if repo else {}),
    }


def test_shared_scope_never_includes_other_alias():
    one = workdir(repo=True)
    two = workdir("Other", runtimes=("claude",))
    aliases, methods = tool.core_scope({"workdirs": [one]}, {"workdirs": [one, two]})
    assert aliases == ("Repo",)
    assert set(methods) == {
        "bridge.ping",
        "runtime.list",
        "repo.inspect",
        "repo.action",
        "repo.commit",
        "repo.push",
    }


def test_runtime_scope_no_repo_methods():
    one = workdir(runtimes=("claude",))
    aliases, methods = tool.core_scope({"workdirs": [one]}, {"workdirs": [one]})
    assert aliases == ("Repo",)
    assert "task.submit" in methods and "runtime.models" in methods
    assert not any(m.startswith("repo.") for m in methods)


def test_review_mode_grant_is_discovery_only():
    one = workdir()
    one["agent_mode"] = "review"
    assert tool.core_scope({"workdirs": [one]}, {"workdirs": [one]}) == (
        ("Repo",),
        ("bridge.ping", "runtime.list"),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"host_path": "C:/other"},
        {"read_only": True},
        {"agent_mode": "review"},
        {"repo_actions": {"different": {}}},
        {"agent_runtimes": ["claude"]},
    ],
)
def test_mismatched_policy_refused(change):
    one = workdir(repo=True)
    with pytest.raises(tool.Refusal):
        tool.core_scope({"workdirs": [one | change]}, {"workdirs": [one]})


def test_shared_runtime_settings_must_match():
    one = workdir(runtimes=("claude",))
    with pytest.raises(tool.Refusal):
        tool.core_scope(
            {"workdirs": [one], "runtimes": {"claude": {"claude_bin": "one"}}},
            {"workdirs": [one], "runtimes": {"claude": {"claude_bin": "two"}}},
        )


@pytest.mark.parametrize(("mode", "identity"), [("owned", "owner"), ("shared", "peer")])
def test_grant_matches_binding_and_is_unique(mode, identity):
    rev = {"Repo": "a" * 64}
    methods = ("bridge.ping", "runtime.list")
    b, g = tool.grant_pair(identity, "owner", mode, rev, methods)
    b2, _ = tool.grant_pair(identity, "owner", mode, rev, methods)
    assert b["bridge_mode"] == mode
    assert b["instance_id"] == g["core_id"] == identity
    assert b["key_id"] == g["key_id"] and b["secret_hex"] == g["secret_hex"]
    assert b["workdirs"] == g["workdirs"]
    assert b["secret_hex"] != b2["secret_hex"] and len(b["secret_hex"]) == 64


def test_native_render_uses_named_resources_and_flat_runtime(tmp_path):
    request = {
        "workdirs": [workdir(runtimes=("claude",))],
        "runtimes": {"claude": {"enabled": True, "use_proxy": False}},
    }
    actual = tool.render_bridge(request, "owner", "sid", tmp_path, "pipe")
    assert actual["bridge_id"] == "owner" and actual["socket_path"] == "pipe"
    assert actual["core_grants_enabled"] is True and actual["lease_key"] == "alias"
    assert actual["claude"] == request["runtimes"]["claude"]
    assert "runtimes" not in actual and actual["enable_fake_runtime"] is False


def test_explicit_proxy_policy_is_preserved_by_named_renderer(tmp_path):
    request = {
        "workdirs": [workdir(runtimes=("claude",))],
        "runtimes": {"claude": {"enabled": True, "use_proxy": True}},
    }
    actual = tool.render_bridge(request, "owner", "sid", tmp_path, "pipe")
    assert actual["claude"]["use_proxy"] is True
    assert "proxy" not in actual  # endpoint arrives only over the private bootstrap


def test_private_proxy_source_refuses_new_peer_and_checkout_roots(tmp_path, monkeypatch):
    from serverfs_mcp import windows_cloudflare_security
    from serverfs_mcp.windows_v25 import storage

    monkeypatch.setattr(windows_cloudflare_security, "_no_reparse_components", lambda p: None)
    monkeypatch.setattr(storage, "check_private", lambda *a, **kw: None)
    checkout = tmp_path / "checkout"
    script = checkout / "deployment/windows/configure-v25-private.py"
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setattr(tool, "__file__", str(script))
    new_root = tmp_path / "new"
    peer_root = tmp_path / "existing"
    private = tmp_path / "private"
    for directory in (new_root, peer_root, private):
        directory.mkdir()
    raw = b"SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170\n"
    for root in (new_root, peer_root, checkout, private):
        (root / "source.env").write_bytes(raw)

    for root in (new_root, peer_root, checkout):
        with pytest.raises(tool.Refusal, match="outside exposed workdirs and checkout"):
            tool.read_private_proxy_source(root / "source.env", [new_root, peer_root], "synthetic")
    normalized = tool.read_private_proxy_source(
        private / "source.env", [new_root, peer_root], "synthetic"
    )
    assert normalized.startswith(raw)
    (private / "source.env").write_bytes(b"x" * 4097)
    with pytest.raises(tool.Refusal, match="oversized"):
        tool.read_private_proxy_source(private / "source.env", [new_root, peer_root], "synthetic")


def test_private_root_boundary(tmp_path):
    home = tmp_path / "ServerFS"
    workdir = home / "v25/instances/alpha/workdirs/Repo"
    workdir.mkdir(parents=True)
    private = home / "v25/instances/alpha/services"
    private.mkdir()
    external = tmp_path / "other"
    external.mkdir()
    tool.disjoint_private_roots([workdir, external], home)
    for forbidden in (tmp_path, home, private, workdir.parent.parent):
        with pytest.raises(tool.Refusal):
            tool.disjoint_private_roots([forbidden], home)


def test_create_only_refuses_existing_bytes(tmp_path):
    path = tmp_path / "secret"
    path.write_bytes(b"keep")
    with pytest.raises(tool.Refusal):
        tool.absent(path)
    assert path.read_bytes() == b"keep"
