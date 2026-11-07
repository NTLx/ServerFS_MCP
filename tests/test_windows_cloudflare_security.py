"""Windows private-credential policy tests; no production ACLs are changed."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp import windows_cloudflare_security as security

CURRENT_SID = "S-1-5-21-100-200-300-1001"
VALID_SDDL = f"O:{CURRENT_SID}G:SYD:P(A;;FA;;;{CURRENT_SID})(A;;FA;;;SY)(A;;FA;;;BA)"


@pytest.fixture
def layout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(security, "_is_windows", lambda: True)
    checkout = tmp_path / "checkout"
    workdir = tmp_path / "workdir"
    private = tmp_path / "private"
    for directory in (checkout, workdir, private):
        directory.mkdir()
    token = private / "token"
    token.write_text("secret-fixture", encoding="utf-8")
    return token, checkout, workdir


def test_validates_current_user_private_file_without_shell(layout):
    token, checkout, workdir = layout
    calls = []

    def read_sddl(path):
        calls.append(path)
        return VALID_SDDL

    output = security.validate_private_file(
        token,
        checkout_root=checkout,
        workdir_roots=[workdir],
        user_sid=CURRENT_SID,
        sddl_reader=read_sddl,
    )
    assert output == token.resolve()
    assert calls == [token.resolve()]


@pytest.mark.parametrize("output", ["", "unexpected", "D:P", "O:S-1-5-18D:P"])
def test_rejects_bad_acl_or_untrusted_response(layout, output):
    token, checkout, workdir = layout
    with pytest.raises(security.WindowsPrivateFileError, match="owner or DACL"):
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
            sddl_reader=lambda path: output,
        )


@pytest.mark.parametrize(
    "sddl",
    [
        VALID_SDDL.replace(f"O:{CURRENT_SID}", "O:S-1-5-21-999-999-999-1001"),
        VALID_SDDL.replace("D:P", "D:"),
        VALID_SDDL.replace("(A;;FA", "(A;ID;FA", 1),
        VALID_SDDL + "(A;;FR;;;WD)",
        VALID_SDDL.replace(f"(A;;FA;;;{CURRENT_SID})", ""),
        VALID_SDDL + "(XA;;FA;;;WD;(@User.Test == 1))",
    ],
)
def test_rejects_foreign_or_ambiguous_sddl(sddl):
    with pytest.raises(security.WindowsPrivateFileError, match="owner or DACL"):
        security._validate_private_sddl(sddl, CURRENT_SID)


def test_native_reader_is_used_without_process_spawn(layout, monkeypatch):
    token, checkout, workdir = layout
    # A dummy loader proves the validator does not call a shell or parse argv.
    monkeypatch.setattr(security, "_read_windows_file_sddl", lambda path: VALID_SDDL)
    assert (
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
            sddl_reader=security._read_windows_file_sddl,
        )
        == token.resolve()
    )


def test_rejects_credential_inside_workdir(layout):
    token, checkout, workdir = layout
    inside = workdir / "origin.env"
    inside.write_text("SERVERFS_HTTP_API_KEY=fixture", encoding="utf-8")
    with pytest.raises(security.WindowsPrivateFileError, match="overlaps"):
        security.validate_private_file(
            inside, checkout_root=checkout, workdir_roots=[workdir], user_sid=CURRENT_SID
        )


def test_rejects_credential_inside_checkout(layout):
    token, checkout, workdir = layout
    inside = checkout / "token"
    inside.write_text("fixture", encoding="utf-8")
    with pytest.raises(security.WindowsPrivateFileError, match="overlaps"):
        security.validate_private_file(
            inside, checkout_root=checkout, workdir_roots=[workdir], user_sid=CURRENT_SID
        )


def test_rejects_missing_file(layout):
    token, checkout, workdir = layout
    with pytest.raises(security.WindowsPrivateFileError, match="unavailable"):
        security.validate_private_file(
            token.with_name("missing"),
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
        )


def test_rejects_invalid_sid(layout):
    token, checkout, workdir = layout
    with pytest.raises(security.WindowsPrivateFileError, match="SID"):
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid="Everyone",
        )


def test_fails_closed_on_native_acl_read_error(layout):
    token, checkout, workdir = layout

    def broken_reader(path):
        raise OSError("sensitive path details")

    with pytest.raises(security.WindowsPrivateFileError, match="verification unavailable") as exc:
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
            sddl_reader=broken_reader,
        )
    assert "sensitive path details" not in str(exc.value)


def test_rejects_reparse_point(layout, monkeypatch):
    token, checkout, workdir = layout

    def fail(path):
        raise security.WindowsPrivateFileError("credential path contains a reparse point")

    monkeypatch.setattr(security, "_no_reparse_components", fail)
    with pytest.raises(security.WindowsPrivateFileError, match="reparse point"):
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
        )


def test_unsupported_platform_rejected(layout, monkeypatch):
    token, checkout, workdir = layout
    monkeypatch.setattr(security, "_is_windows", lambda: False)
    with pytest.raises(security.WindowsPrivateFileError, match="requires Windows"):
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
        )
