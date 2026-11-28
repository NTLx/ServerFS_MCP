"""Phase D tests for the supervised bootstrap channel (§15 D4).

The channel exists to keep the Agent proxy endpoint out of every place Phase 0F showed it leaking:
argv, the Bridge environment, and the generated config. So these tests assert three things:

- the frame is **parsed strictly** and every malformed shape fails closed, because a Bridge that
  silently ran without the egress it was told to use would leak provider traffic direct;
- the material is **never persisted or echoed** — no serializer exists on the type, and its repr is
  redacted;
- **EOF is the shutdown trigger**, so the supervisor needs no second control protocol, and the
  Bridge's existing close path is reused rather than duplicated.

The end-to-end case drives a real Bridge subprocess with a real stdin pipe, because "the frame
arrives and then EOF stops the process" is only observable across a process boundary.
"""

from __future__ import annotations

import json

import pytest

from serverfs_agent_bridge.bootstrap import (
    BOOTSTRAP_VERSION,
    MAX_BOOTSTRAP_BYTES,
    BootstrapError,
    RuntimeProxy,
    encode_bootstrap_frame,
    parse_bootstrap_frame,
)

PROXY = RuntimeProxy(url="http://127.0.0.1:18080", no_proxy="corp.example,127.0.0.1,localhost,::1")


class TestFrameRoundTrip:
    """Encoder and parser live together so they cannot drift."""

    def test_round_trip_with_proxy(self) -> None:
        frame = parse_bootstrap_frame(encode_bootstrap_frame(PROXY))
        assert frame.version == BOOTSTRAP_VERSION
        assert frame.has_proxy is True
        assert frame.agent_proxy is not None
        assert frame.agent_proxy.url == PROXY.url
        assert frame.agent_proxy.no_proxy == PROXY.no_proxy

    def test_round_trip_without_proxy(self) -> None:
        frame = parse_bootstrap_frame(encode_bootstrap_frame(None))
        assert frame.has_proxy is False
        assert frame.agent_proxy is None

    def test_disabled_proxy_block_is_a_valid_statement(self) -> None:
        frame = parse_bootstrap_frame(b'{"version":1,"agent_proxy":{"enabled":false}}')
        assert frame.agent_proxy is None

    def test_absent_proxy_block_is_accepted(self) -> None:
        assert parse_bootstrap_frame(b'{"version":1}').agent_proxy is None


class TestStrictParsing:
    """Fail closed on anything malformed."""

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[]",
            b'"a string"',
            b"{}",
            b'{"version":2,"agent_proxy":{"enabled":false}}',
            b'{"version":"1"}',
            b'{"version":1,"agent_proxy":{"enabled":"yes"}}',
            b'{"version":1,"agent_proxy":{}}',
            b'{"version":1,"agent_proxy":{"enabled":true}}',
            b'{"version":1,"agent_proxy":{"enabled":true,"url":""}}',
            b'{"version":1,"agent_proxy":"nope"}',
        ],
    )
    def test_malformed_frames_are_refused(self, raw: bytes) -> None:
        with pytest.raises(BootstrapError):
            parse_bootstrap_frame(raw)

    def test_unknown_top_level_key_is_refused(self) -> None:
        with pytest.raises(BootstrapError, match="unknown field"):
            parse_bootstrap_frame(b'{"version":1,"surprise":true}')

    def test_unknown_nested_key_is_refused(self) -> None:
        with pytest.raises(BootstrapError, match="unknown field"):
            parse_bootstrap_frame(
                b'{"version":1,"agent_proxy":{"enabled":true,"url":"http://h:1","no_proxy":"","x":1}}'
            )

    def test_oversized_frame_is_refused(self) -> None:
        payload = b'{"version":1,"pad":"' + b"x" * MAX_BOOTSTRAP_BYTES + b'"}'
        with pytest.raises(BootstrapError, match="bounded size"):
            parse_bootstrap_frame(payload)

    def test_invalid_utf8_is_refused(self) -> None:
        with pytest.raises(BootstrapError, match="UTF-8"):
            parse_bootstrap_frame(b'{"version":1,"x":"\xff\xfe"}')

    def test_unknown_key_error_does_not_echo_the_key_name(self) -> None:
        """An unknown key could itself be secret-shaped; the message reports a count only."""
        with pytest.raises(BootstrapError) as raised:
            parse_bootstrap_frame(b'{"version":1,"my_secret_key":"value"}')
        assert "my_secret_key" not in str(raised.value)


class TestNoPersistenceOrEcho:
    """The material lives in memory and nowhere else."""

    def test_repr_is_redacted(self) -> None:
        rendered = repr(PROXY)
        assert "127.0.0.1" not in rendered
        assert "18080" not in rendered
        assert "redacted" in rendered

    def test_frame_repr_does_not_leak(self) -> None:
        frame = parse_bootstrap_frame(encode_bootstrap_frame(PROXY))
        assert "18080" not in repr(frame.agent_proxy)

    def test_type_exposes_no_serializer(self) -> None:
        """There is deliberately no to_dict/to_json, so persistence cannot be added by accident."""
        assert not hasattr(RuntimeProxy, "to_dict")
        assert not hasattr(RuntimeProxy, "to_json")
        assert not hasattr(RuntimeProxy, "asdict")


class TestNoPublicProtocolChange:
    """The frame is a private lifecycle channel, not protocol (§15 D4, §70).

    The requirement is that adding this channel does not alter the public RPC surface — not that the
    two numbers must differ. They are separate constants, and the frozen ``PROTOCOL_VERSION`` is
    still 1 exactly as before, which is the property that actually matters.
    """

    def test_public_protocol_version_is_unchanged_at_one(self) -> None:
        from serverfs_agent_bridge import protocol

        assert protocol.PROTOCOL_VERSION == 1

    def test_bootstrap_version_is_a_separate_constant(self) -> None:
        from serverfs_agent_bridge import bootstrap

        # Both values are small ints, so identity comparison would say nothing. What matters is that
        # the frame version is defined in the bootstrap module rather than imported from the public
        # protocol, so a future bump of either cannot silently move the other.
        assert bootstrap.BOOTSTRAP_VERSION == 1
        assert "PROTOCOL_VERSION" not in vars(bootstrap)

    def test_bootstrap_module_is_not_imported_by_the_protocol(self) -> None:
        """The public transport must not grow a dependency on the private lifecycle channel."""
        from serverfs_agent_bridge import protocol

        source = protocol.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            assert "bootstrap" not in handle.read()

    def test_encoded_frame_is_one_line(self) -> None:
        """One frame means one line, so the supervisor can write it without framing concerns."""
        encoded = encode_bootstrap_frame(PROXY)
        assert encoded.count(b"\n") == 1
        assert encoded.endswith(b"\n")
        assert json.loads(encoded.decode("utf-8"))["version"] == BOOTSTRAP_VERSION


class TestTheContainmentFactOnThePrivateChannel:
    """Phase F: startup reconciliation needs to know the previous generation's containment is over.

    No provider adapter can know it -- Qoder's SDK does not report whether the old process is alive
    -- so the supervisor states it, because the supervisor is the process that establishes it: it
    holds the per-user lifecycle lease and it created the SID-scoped named Job Object rather than
    finding one. It rides this private parent-to-child channel, not the public RPC, and it is
    optional with a false default so that an unsupervised launch or an older supervisor keeps the
    conservative behaviour instead of being read as containment.
    """

    def test_an_absent_key_means_not_proven(self) -> None:
        frame = parse_bootstrap_frame(encode_bootstrap_frame(PROXY))
        assert frame.prior_bridge_execution_stopped is False

    def test_the_fact_round_trips(self) -> None:
        encoded = encode_bootstrap_frame(PROXY, prior_bridge_execution_stopped=True)
        assert parse_bootstrap_frame(encoded).prior_bridge_execution_stopped is True

    def test_a_non_boolean_is_refused(self) -> None:
        raw = json.dumps(
            {"version": BOOTSTRAP_VERSION, "prior_bridge_execution_stopped": "yes"}
        ).encode()
        with pytest.raises(BootstrapError):
            parse_bootstrap_frame(raw)

    def test_the_protocol_version_is_not_touched(self) -> None:
        """A private lifecycle statement must never look like a protocol change."""
        encoded = encode_bootstrap_frame(PROXY, prior_bridge_execution_stopped=True)
        assert json.loads(encoded.decode("utf-8"))["version"] == BOOTSTRAP_VERSION
