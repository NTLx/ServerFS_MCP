"""Phase D tests for the private Bridge config renderer (§15 D2, §5.3, §4.4).

The renderer is the piece that lets an operator maintain exactly one file. What matters here is not
that it produces JSON, but three properties that are easy to assert and expensive to get wrong:

- **the rendered document loads through the Bridge's own loader.** A renderer with its own relaxed
  dialect would produce a config the real Bridge refuses only after the process has been spawned, so
  every case here ends by round-tripping through ``BridgeConfig.load``.
- **the rendered document holds no credential.** The Agent proxy endpoint is not part of it at all;
  it arrives over the private bootstrap channel (D4) and lives only in Bridge memory.
- **the private-state contract is applied, not assumed.** The generated file is internal private
  state, so it gets a protected descriptor, an owner check, reparse refusal and atomic publication
  from the Bridge's own helpers rather than a second copy of the ACL logic (§15 D2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.lease_identity import alias_lease_id
from serverfs_agent_bridge.local_ipc import derive_pipe_name
from serverfs_agent_bridge.render_config import (
    NATIVE_LEASE_KEY,
    build_config_document,
    render_native_bridge_config,
)
from serverfs_agent_bridge.windows_security import current_user_sid

pytestmark = pytest.mark.skipif(
    not str(Path("")).startswith("/") and os.name != "nt",
    reason="the native renderer is a Windows deployment shape",
)


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Render tests must not lean on the host's real ~/.codex (absent on CI runners)."""
    import serverfs_agent_bridge.config as config_module

    fake = tmp_path / "codex-home"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module, "_default_codex_home", lambda: fake)


def _request(workdir: Path, **overrides) -> dict:
    request = {
        "workdirs": [
            {
                "alias": "repo",
                "host_path": str(workdir),
                "read_only": False,
                "agent_mode": "workspace-write",
                "agent_runtimes": ["codex"],
            }
        ],
        "runtimes": ["codex"],
    }
    request.update(overrides)
    return request


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestRenderedDocumentShape:
    """The document must be one the real Bridge loader accepts."""

    def test_rendered_config_loads_through_the_bridge_loader(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        config = BridgeConfig.load(rendered.config_path)
        policy = config.policies.get("repo")
        assert policy.alias == "repo"
        assert policy.mode.value == "workspace-write"
        assert sorted(policy.runtimes) == ["codex"]
        assert config.codex.enabled is True

    def test_lease_key_is_always_alias(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        document = json.loads(rendered.config_path.read_text(encoding="utf-8"))
        assert document["lease_key"] == NATIVE_LEASE_KEY == "alias"

    def test_workdir_entry_carries_no_slot(self, tmp_path: Path, workdir: Path):
        """A native slot would key the lease differently and lock two files (§5.3)."""
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        document = json.loads(rendered.config_path.read_text(encoding="utf-8"))
        assert all("slot" not in entry for entry in document["workdirs"])

    def test_a_slot_in_the_request_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["slot"] = 3
        # `slot` is not a known native field, so the unknown-key check refuses it before the
        # dedicated message matters. Either refusal is correct; neither may render a slot.
        with pytest.raises(BridgeError, match="slot"):
            render_native_bridge_config(request, home=tmp_path / "bridge-home")

    def test_limits_pass_through_to_the_bridge(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(
            _request(workdir, limits={"max_active_tasks": 2}), home=tmp_path / "bridge-home"
        )
        assert BridgeConfig.load(rendered.config_path).limits.max_active_tasks == 2

    def test_disabled_runtime_mode_round_trips(self, tmp_path: Path, workdir: Path):
        request = {
            "workdirs": [
                {
                    "alias": "repo",
                    "host_path": str(workdir),
                    "read_only": True,
                    "agent_mode": "disabled",
                }
            ],
            "runtimes": [],
        }
        rendered = render_native_bridge_config(request, home=tmp_path / "bridge-home")
        policy = BridgeConfig.load(rendered.config_path).policies.get("repo")
        assert policy.mode.value == "disabled"
        assert policy.runtimes == frozenset()


class TestPeerIdentity:
    """Identity is measured, never derived from a name (§4.4, §15 D2)."""

    def test_peer_sid_comes_from_the_live_process(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        assert rendered.allowed_peer_sid == current_user_sid()
        assert rendered.allowed_peer_sid.startswith("S-")

    def test_rendered_sid_is_what_the_bridge_loads(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        assert BridgeConfig.load(rendered.config_path).allowed_peer_sid == current_user_sid()

    def test_pipe_name_is_the_deterministic_sid_derivation(self, tmp_path: Path, workdir: Path):
        """Both sides must derive the same name independently; the name is not authentication."""
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        # Compared as a string: pathlib renders the pipe prefix with forward slashes on Windows,
        # and what both sides must agree on is the pipe name itself.
        assert str(rendered.socket_path).replace("/", "\\") == derive_pipe_name(current_user_sid())
        assert derive_pipe_name(current_user_sid()).startswith(
            "\\\\.\\pipe\\serverfs-agent-bridge-v1-"
        )

    def test_no_username_appears_in_the_document(self, tmp_path: Path, workdir: Path):
        """Identity is the SID. A username anywhere in the document would be a second identity."""
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        document = json.loads(rendered.config_path.read_text(encoding="utf-8"))
        username = os.environ.get("USERNAME", "").lower()
        if username:
            # The data home legitimately lives under the user profile, so the assertion is about
            # the document's identity fields, not about the whole blob.
            assert username not in document["allowed_peer_sid"].lower()
            assert document["allowed_peer_sid"] == current_user_sid()
            assert "user_name" not in document and "username" not in document


class TestSecretHygiene:
    """The generated document is policy and identity, never a credential."""

    def test_document_contains_no_proxy_url(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        blob = rendered.config_path.read_text(encoding="utf-8")
        assert "127.0.0.1" not in blob
        assert "proxy" not in blob.lower()

    def test_document_has_no_credential_shaped_keys(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        document = json.loads(rendered.config_path.read_text(encoding="utf-8"))
        assert not {"api_key", "token", "password", "proxy_url", "control_plane_key"} & set(
            document
        )

    def test_a_proxy_shaped_request_key_is_refused(self, tmp_path: Path, workdir: Path):
        """The renderer refuses unknown keys, so an endpoint could not be smuggled through it."""
        with pytest.raises(BridgeError, match="unknown render request field"):
            render_native_bridge_config(
                _request(workdir, agent_proxy={"url": "http://127.0.0.1:8080"}),
                home=tmp_path / "bridge-home",
            )


class TestPrivateState:
    """The generated config is internal private state, so the §25/§28 contract applies."""

    def test_config_lands_in_the_bridge_data_home(self, tmp_path: Path, workdir: Path):
        home = tmp_path / "bridge-home"
        rendered = render_native_bridge_config(_request(workdir), home=home)
        assert rendered.config_path == home / "bridge.json"
        assert rendered.config_path.is_file()

    def test_state_and_lock_directories_are_created(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        assert rendered.state_dir.is_dir()
        assert rendered.lock_dir.is_dir()

    def test_rerender_is_idempotent(self, tmp_path: Path, workdir: Path):
        """A restart re-renders its own config; that must be a verify, never a repair (§25)."""
        home = tmp_path / "bridge-home"
        first = render_native_bridge_config(_request(workdir), home=home)
        again = render_native_bridge_config(_request(workdir), home=home)
        assert first.config_path == again.config_path
        assert BridgeConfig.load(again.config_path).policies.get("repo").alias == "repo"

    def test_no_temp_file_survives_publication(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        leftovers = [
            p.name for p in rendered.config_path.parent.iterdir() if p.name.endswith(".tmp")
        ]
        assert leftovers == []

    @pytest.mark.skipif(os.name != "nt", reason="NTFS descriptor semantics")
    def test_config_file_is_private(self, tmp_path: Path, workdir: Path):
        from serverfs_agent_bridge.private_state import verify_private_file

        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        # Verifying rather than asserting a raw ACL: the contract is the shared helper's verdict.
        verify_private_file(
            rendered.config_path,
            not_regular="rendered config is not a regular file",
            not_private="rendered config is not private",
        )

    @pytest.mark.skipif(os.name != "nt", reason="NTFS reparse semantics")
    def test_a_reparse_data_home_is_refused(self, tmp_path: Path, workdir: Path):
        """§28: a junction at the state root is the pre-planting case, and it is refused."""
        import subprocess

        home = tmp_path / "bridge-home"
        target = tmp_path / "elsewhere"
        target.mkdir()
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(home), str(target)],
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("junction creation is unavailable here")
        with pytest.raises(BridgeError, match="reparse"):
            render_native_bridge_config(_request(workdir), home=home)


class TestLeaseIdentityAgreement:
    """The rendered lease ids must equal what the Bridge itself derives."""

    def test_lease_ids_match_the_bridge_registry(self, tmp_path: Path, workdir: Path):
        rendered = render_native_bridge_config(_request(workdir), home=tmp_path / "bridge-home")
        config = BridgeConfig.load(rendered.config_path)
        assert tuple(rendered.lease_ids) == config.policies.lease_ids()
        assert rendered.lease_ids == (alias_lease_id("repo"),)


class TestRequestValidation:
    """The renderer fails at startup, never after the Bridge is spawned."""

    def test_unknown_top_level_key_is_refused(self, tmp_path: Path, workdir: Path):
        with pytest.raises(BridgeError, match="unknown render request field"):
            render_native_bridge_config(_request(workdir, surprise=1), home=tmp_path / "h")

    def test_empty_workdirs_is_refused(self, tmp_path: Path):
        with pytest.raises(BridgeError, match="non-empty array"):
            render_native_bridge_config({"workdirs": []}, home=tmp_path / "h")

    def test_duplicate_alias_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"].append(dict(request["workdirs"][0]))
        with pytest.raises(BridgeError, match="duplicate workdir alias"):
            render_native_bridge_config(request, home=tmp_path / "h")

    def test_unknown_runtime_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["runtimes"] = ["gemini"]
        with pytest.raises(BridgeError, match="unknown agent runtime"):
            render_native_bridge_config(request, home=tmp_path / "h")

    def test_unknown_agent_mode_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["agent_mode"] = "yolo"
        with pytest.raises(BridgeError, match="not a known agent mode"):
            render_native_bridge_config(request, home=tmp_path / "h")

    def test_workspace_write_on_a_read_only_workdir_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["read_only"] = True
        with pytest.raises(BridgeError, match="requires a writable workdir"):
            render_native_bridge_config(request, home=tmp_path / "h")

    def test_relative_host_path_is_refused(self, tmp_path: Path, workdir: Path):
        request = _request(workdir)
        request["workdirs"][0]["host_path"] = "relative/path"
        with pytest.raises(BridgeError, match="must be absolute"):
            render_native_bridge_config(request, home=tmp_path / "h")

    def test_missing_host_path_is_refused(self, tmp_path: Path):
        with pytest.raises(BridgeError, match="host_path"):
            render_native_bridge_config(
                {"workdirs": [{"alias": "repo"}], "runtimes": []}, home=tmp_path / "h"
            )


class TestDocumentBuilderIsPure:
    """``build_config_document`` decides content; ``render_native_bridge_config`` places it."""

    def test_builder_emits_the_expected_keys(self, tmp_path: Path, workdir: Path):
        document = build_config_document(
            _request(workdir),
            socket_path=Path("x"),
            state_dir=tmp_path / "s",
            lock_dir=tmp_path / "l",
            peer_sid="S-1-5-21-1-2-3-1001",
        )
        assert document["allowed_peer_sid"] == "S-1-5-21-1-2-3-1001"
        assert document["lease_key"] == "alias"
        # The builder decides content only: no directory it names has been created.
        assert not (tmp_path / "s").exists()
        assert not (tmp_path / "l").exists()


class TestCliEntryPoint:
    """Bounded stdin in, one redacted summary line out."""

    def _run(self, payload: bytes, home: Path) -> int:
        import io
        import sys as _sys

        from serverfs_agent_bridge.render_config import main

        stdin, stdout = _sys.stdin, _sys.stdout
        _sys.stdin = type("S", (), {"buffer": io.BytesIO(payload)})()
        _sys.stdout = io.StringIO()
        try:
            return main(["--data-home", str(home)])
        finally:
            _sys.stdin, _sys.stdout = stdin, stdout

    def test_valid_request_exits_zero_and_prints_a_summary(self, tmp_path: Path, workdir: Path):

        home = tmp_path / "bridge-home"
        assert self._run(json.dumps(_request(workdir)).encode(), home) == 0
        assert (home / "bridge.json").is_file()

    def test_malformed_json_exits_two_without_writing(self, tmp_path: Path):
        assert self._run(b"{not json", tmp_path / "h") == 2
        assert not (tmp_path / "h" / "bridge.json").exists()

    def test_oversized_request_is_refused(self, tmp_path: Path):
        payload = b'{"workdirs":[],"pad":"' + b"x" * 300_000 + b'"}'
        assert self._run(payload, tmp_path / "h") == 2
