"""Pure, side-effect-free checks for Windows W3 old-key newline compatibility.

Never open the real key. The bootstrap file is evaluated with run_name that
does not invoke its guarded Windows main() entrypoint.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def validator():
    script = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "windows"
        / "v0250-w3-openai-manifest-bootstrap.py"
    )
    return runpy.run_path(str(script))["validated_legacy_key_payload"]


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_bootstrap_accepts_regular_legacy_key_with_optional_trailing_newline(
    validator,
    ending: bytes,
) -> None:
    payload = b"example_legacy_credential_with_48_bytes____________"
    assert validator(payload + ending) == payload


@pytest.mark.parametrize(
    "extra",
    [
        b"\r",
        b"\r\r\n",
        b"\n\r\n",
        b"\r\n\r\n",
        b"\ninside",
        b"middle\rdata",
        b"\x00",
        b"\n\x00",
    ],
)
def test_bootstrap_rejects_legacy_key_with_embedded_control_characters(
    validator,
    extra: bytes,
) -> None:
    data = b"example_legacy_credential_with_48_bytes____________" + extra
    with pytest.raises(ValueError, match="protected source differs"):
        validator(data)


@pytest.mark.parametrize("value", [b"", b"tiny", b"x" * 4097])
def test_bootstrap_rejects_out_of_range_keys(validator, value: bytes) -> None:
    with pytest.raises(ValueError, match="protected source differs"):
        validator(value)
