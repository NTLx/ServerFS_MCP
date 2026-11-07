"""Windows private-credential policy tests; no production ACLs are changed."""

from __future__ import annotations

import subprocess
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


def test_validates_current_user_private_file_without_secret_in_argv(layout):
    token, checkout, workdir = layout
    calls = []

    def signed_acl_runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, VALID_SDDL + "\n", "")

    output = security.validate_private_file(
        token,
        checkout_root=checkout,
        workdir_roots=[workdir],
        user_sid=CURRENT_SID,
        runner=signed_acl_runner,
    )
    assert output == token.resolve()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    assert "secret-fixture" not in " ".join(argv)
    assert str(token) not in " ".join(argv)
    assert "SERVERFS_PRIVATE_FILE_SID" not in kwargs["env"]
    assert kwargs["env"]["SERVERFS_PRIVATE_FILE_PATH"] == str(token.resolve())


@pytest.mark.parametrize("code,output", [(11, ""), (13, ""), (0, "unexpected")])
def test_rejects_bad_acl_or_untrusted_response(layout, code, output):
    token, checkout, workdir = layout

    def reject(args, **kwargs):
        return subprocess.CompletedProcess(args, code, output, "")

    with pytest.raises(security.WindowsPrivateFileError, match="owner or DACL"):
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
            runner=reject,
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


def test_acl_read_script_is_compatible_with_constrained_powershell():
    assert "Get-Acl" in security._READ_SDDL_SCRIPT
    assert ".GetOwner(" not in security._READ_SDDL_SCRIPT
    assert ".GetAccessRules(" not in security._READ_SDDL_SCRIPT


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


def test_fails_closed_on_power_shell_error(layout):
    token, checkout, workdir = layout

    def broken_runner(*args, **kwargs):
        raise OSError("sensitive path details")

    with pytest.raises(security.WindowsPrivateFileError, match="verification unavailable") as exc:
        security.validate_private_file(
            token,
            checkout_root=checkout,
            workdir_roots=[workdir],
            user_sid=CURRENT_SID,
            runner=broken_runner,
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
