"""Windows security-descriptor primitives behind the private-state seam (§3, §6.1).

Narrow ctypes usage, following the same discipline as ``serverfs_mcp.doctor``: public
kernel32/advapi32 entry points, explicit ``restype``/``argtypes``, and no wrapper that
hides an error code. Every helper here answers one of the questions the private-state
contract asks about an existing object — who owns it, is its DACL explicit, who is
granted access, is it a reparse point — and the caller decides whether the answer is
acceptable. Nothing in this module repairs an object it did not create (§25): the
Bridge creates new state with an explicit descriptor and only verifies objects that
already exist.

Measured on WorkPC (Windows 11 x64 + NTFS, one interactive account):

- ``GetNamedSecurityInfoW`` with ``OWNER_SECURITY_INFORMATION |
  DACL_SECURITY_INFORMATION`` succeeds from an ordinary unelevated process on its own
  files; requesting the SACL bit (0x8) instead fails with 1314
  ``ERROR_PRIVILEGE_NOT_HELD``, which is why the masks below are named explicitly.
- ``SetNamedSecurityInfoW``'s sixth parameter is a **PACL**, not a security descriptor:
  passing a descriptor there returns 0 (success) and leaves ``D:PAI`` — a protected,
  empty DACL that locks the owner out of its own directory. This module therefore only
  applies descriptors at creation time through ``SECURITY_ATTRIBUTES``.
- A directory created with ``D:P(A;OICI;GA;;;<sid>)`` reads back as owner==<sid>,
  protected, exactly one ACE, and a file created inside it inherits
  ``(A;ID;FA;;;<sid>)`` with no other trustee.
- A junction reports ``st_reparse_tag`` while ``S_ISLNK`` is false and ``is_dir()`` is
  true, so reparse detection must use the tag, not the POSIX mode bits.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import stat
from ctypes import wintypes

from .errors import BridgeError

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004
SACL_SECURITY_INFORMATION = 0x00000008

SE_DACL_PRESENT = 0x0004
SE_DACL_PROTECTED = 0x1000

SDDL_REVISION_1 = 1
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x00000080
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
READ_CONTROL = 0x00020000
FILE_SHARE_ALL = 0x00000007
ERROR_ALREADY_EXISTS = 183
ERROR_FILE_EXISTS = 80
ERROR_ACCESS_DENIED = 5
FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Trustees that must never hold an access grant on Bridge private state.
BANNED_TRUSTEES = {
    "S-1-1-0": "Everyone",
    "S-1-5-2": "Network",
    "S-1-5-7": "Anonymous",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "BUILTIN\\Users",
    "S-1-5-32-546": "BUILTIN\\Guests",
}

ACE_ACCESS_ALLOWED = 0
ACE_ACCESS_DENIED = 1

# The built-in trustees an inherited descriptor on a normal NTFS volume carries besides the
# user, measured on this machine: a directory created in %TEMP% inherits SYSTEM, Administrators
# and SELF, and SELF resolves to whoever owns the object — which §26 has already asserted is the
# Bridge user. They are tolerated only on an object the Bridge did not create: the Bridge's own
# descriptor is written from a SDDL naming the Bridge user alone and protected from inheritance,
# so a state object it just created can never show these. An administrator can read the user's
# files regardless, so refusing these three would stop a legitimate deployment on a pre-created
# directory without adding protection, while a *foreign user* SID — which real %LOCALAPPDATA%
# descriptors on a shared machine do carry — remains the pre-planting signal §25 is about.
SYSTEM_TRUSTEES = {
    "S-1-5-18": "NT AUTHORITY\\SYSTEM",
    "S-1-5-32-544": "BUILTIN\\Administrators",
    "S-1-3-4": "NT AUTHORITY\\SELF",
}
# Kept as aliases because the ACE type byte is what the enumeration compares.
_ACCESS_ALLOWED_ACE_TYPE = ACE_ACCESS_ALLOWED


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


def _load() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.WORD),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.FormatMessageW.restype = wintypes.DWORD
    kernel32.FormatMessageW.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_wchar_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return kernel32, advapi32


KERNEL32, ADVAPI32 = _load()

TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
# TokenOwner: the default owner new objects created by this token get (Windows
# assigns file ownership from this field, not from TokenUser -- an elevated or
# service process creates objects owned by the Administrators group).
TOKEN_OWNER_CLASS = 4


def last_error() -> int:
    return ctypes.get_last_error()


def winerror(code: int | None = None) -> str:
    """A plain-English Win32 message, so a creation failure says which step refused.

    ``format_messages`` is deliberately not used: it raises on the very codes that matter
    here (for example ERROR_FILE_EXISTS), and the message text is diagnostic only.
    """
    if code is None:
        code = last_error()
    if not code:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    length = KERNEL32.FormatMessageW(
        FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        None,
        code,
        0,
        buffer,
        len(buffer),
        None,
    )
    if not length:
        return f"Win32 error {code}"
    return buffer.value.strip() or f"Win32 error {code}"


def sid_to_string(sid: int) -> str | None:
    if not sid:
        return None
    text = wintypes.LPWSTR()
    if not ADVAPI32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
        return None
    try:
        return ctypes.wstring_at(text)
    finally:
        KERNEL32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_token_owner_sid() -> str:
    """The TokenOwner SID of this process, as the canonical ``S-1-…`` string.

    Windows assigns newly created objects to this SID -- not to the TokenUser.
    For a normal user token the two are identical; for an elevated or service
    process the token carries ``SE_GROUP_OWNER`` on Administrators and new
    objects are owned by ``S-1-5-32-544``. Private-state ownership assertions
    must compare against this field or a correct deployment fails its own
    check (measured on the GitHub Windows runner, Phase H).
    """
    token = wintypes.HANDLE()
    if not ADVAPI32.OpenProcessToken(
        KERNEL32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise BridgeError(
            "BRIDGE_IDENTITY_UNAVAILABLE",
            "the process token owner could not be read: token unavailable",
        )
    try:
        needed = wintypes.DWORD()
        ADVAPI32.GetTokenInformation(token, TOKEN_OWNER_CLASS, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not ADVAPI32.GetTokenInformation(
            token, TOKEN_OWNER_CLASS, buffer, needed, ctypes.byref(needed)
        ):
            raise BridgeError("BRIDGE_IDENTITY_UNAVAILABLE", "the token owner could not be read")
        # TOKEN_OWNER is a bare PSID (no SID_AND_ATTRIBUTES wrapper).
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        text = sid_to_string(sid)
        if not text:
            raise BridgeError(
                "BRIDGE_IDENTITY_UNAVAILABLE", "the token owner could not be converted"
            )
        return text
    finally:
        KERNEL32.CloseHandle(token)


def current_user_sid() -> str:
    """The TokenUser SID of this process, as the canonical ``S-1-…`` string."""
    token = wintypes.HANDLE()
    if not ADVAPI32.OpenProcessToken(
        KERNEL32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise BridgeError(
            "BRIDGE_IDENTITY_UNAVAILABLE",
            "the Bridge user SID could not be read from this process token",
        )
    try:
        needed = wintypes.DWORD()
        ADVAPI32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not ADVAPI32.GetTokenInformation(
            token, TOKEN_USER_CLASS, buffer, needed, ctypes.byref(needed)
        ):
            raise BridgeError(
                "BRIDGE_IDENTITY_UNAVAILABLE",
                "the Bridge user SID could not be read from this token",
            )
        # TOKEN_USER is { SID_AND_ATTRIBUTES { PSID Sid; ULONG Attributes } }
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        text = sid_to_string(sid)
        if not text:
            raise BridgeError(
                "BRIDGE_IDENTITY_UNAVAILABLE", "the Bridge user SID could not be converted"
            )
        return text
    finally:
        KERNEL32.CloseHandle(token)


def security_attributes(sddl: str) -> SecurityAttributes:
    """A ``SECURITY_ATTRIBUTES`` carrying ``sddl`` for use at object creation time."""
    descriptor = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not ADVAPI32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(size)
    ):
        raise BridgeError("INVALID_PRIVATE_STATE_DESCRIPTOR", "the security descriptor was refused")
    attributes = SecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = descriptor.value
    attributes.bInheritHandle = False
    return attributes


def private_state_sddl(user_sid: str) -> str:
    """The frozen state-tree descriptor: explicit, protected, one trustee (§6.1, §27).

    ``GA`` (generic all) is what the Bridge needs on its own state, and ``OICI`` is what
    makes a SQLite-written ``-wal``/``-shm`` companion inherit the same single trustee
    instead of the parent's default grant list (measured in Phase B probes).
    """
    if not user_sid.startswith("S-1-"):
        raise BridgeError("INVALID_PRIVATE_STATE_DESCRIPTOR", "the trustee SID is not canonical")
    return f"D:P(A;OICI;GA;;;{user_sid})"


def pipe_dacl_sddl(user_sid: str) -> str:
    """The frozen pipe descriptor: explicit, protected, one trustee (§4.7)."""
    if not user_sid.startswith("S-1-"):
        raise BridgeError("INVALID_PIPE_DESCRIPTOR", "the trustee SID is not canonical")
    return f"D:P(A;;GA;;;{user_sid})"


class ObjectSecurity:
    """What the security descriptor of one existing object actually says."""

    __slots__ = ("owner_sid", "dacl_present", "dacl_protected", "aces")

    def __init__(self, owner_sid: str | None, dacl_present: bool, dacl_protected: bool, aces: list):
        self.owner_sid = owner_sid
        self.dacl_present = dacl_present
        self.dacl_protected = dacl_protected
        self.aces = aces

    def grants(self, sid: str) -> bool:
        return any(ace["sid"] == sid and ace["type"] == ACE_ACCESS_ALLOWED for ace in self.aces)

    def broad_trustee(self) -> str | None:
        for ace in self.aces:
            if ace["type"] != ACE_ACCESS_ALLOWED:
                continue
            banned = BANNED_TRUSTEES.get(ace["sid"] or "")
            if banned is not None:
                return banned
        return None


def read_object_security(path: pathlib.Path) -> ObjectSecurity:
    """Read owner + DACL of an existing path. Failure raises; it never assumes safety (§27)."""
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = ADVAPI32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if code != 0:
        raise BridgeError(
            "PRIVATE_STATE_CHECK_FAILED", "the object security descriptor could not be read"
        )
    try:
        control = wintypes.WORD()
        revision = wintypes.WORD()
        if not ADVAPI32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise BridgeError(
                "PRIVATE_STATE_CHECK_FAILED", "the security descriptor control could not be read"
            )
        present = wintypes.BOOL()
        raw_dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not ADVAPI32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(raw_dacl), ctypes.byref(defaulted)
        ):
            raise BridgeError(
                "PRIVATE_STATE_CHECK_FAILED", "the security descriptor DACL could not be read"
            )
        return ObjectSecurity(
            owner_sid=sid_to_string(owner.value),
            dacl_present=bool(control.value & SE_DACL_PRESENT),
            dacl_protected=bool(control.value & SE_DACL_PROTECTED),
            aces=_read_aces(raw_dacl.value),
        )
    finally:
        if descriptor.value:
            KERNEL32.LocalFree(descriptor)


def _read_aces(dacl: int) -> list[dict]:
    if not dacl:
        return []
    size = AclSizeInformation()
    # AclSizeInformation == 2
    if not ADVAPI32.GetAclInformation(
        ctypes.c_void_p(dacl), ctypes.byref(size), ctypes.sizeof(size), 2
    ):
        raise BridgeError("PRIVATE_STATE_CHECK_FAILED", "the DACL could not be enumerated")
    rows: list[dict] = []
    for index in range(size.AceCount):
        ace = ctypes.c_void_p()
        if not ADVAPI32.GetAce(ctypes.c_void_p(dacl), index, ctypes.byref(ace)):
            raise BridgeError("PRIVATE_STATE_CHECK_FAILED", "a DACL ACE could not be read")
        header = AceHeader.from_address(ace.value)
        # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: header(4) + AccessMask(4) + SidStart(…)
        mask = ctypes.c_uint32.from_address(ace.value + 4).value
        rows.append(
            {
                "type": header.AceType,
                "flags": header.AceFlags,
                "mask": mask,
                "sid": sid_to_string(ace.value + 8),
            }
        )
    return rows


def is_reparse_point(path: pathlib.Path) -> bool:
    """True when ``path`` itself is a reparse point (junction, symlink, mount point).

    ``S_ISLNK`` is false and ``Path.is_dir()`` is true for a junction, so the reparse
    tag from ``lstat`` is the only reliable signal available without a handle (§28).
    """
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        # Nothing is there to reject; the caller's own create step reports what happened.
        return False
    except OSError as exc:
        raise BridgeError(
            "PRIVATE_STATE_CHECK_FAILED", "the state path could not be inspected"
        ) from exc
    tag = getattr(stat_result, "st_reparse_tag", 0)
    if tag:
        return True
    return bool(getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def regular_entry(stat_result: os.stat_result) -> bool:
    """Whether an ``lstat``/``fstat`` names a real file: regular, and not a reparse point.

    A junction reports ``st_reparse_tag`` with ``S_ISLNK`` false, so the POSIX mode bits alone
    would call one an ordinary entry (§28).
    """
    if getattr(stat_result, "st_reparse_tag", 0):
        return False
    if getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return stat.S_ISREG(stat_result.st_mode)


def create_private_directory(path: pathlib.Path, sddl: str) -> bool:
    """Create one directory with ``sddl`` applied at creation time.

    Returns False when the directory already exists (the caller then verifies instead of
    repairing, §25). Any other failure raises.
    """
    attributes = security_attributes(sddl)
    if KERNEL32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
        return True
    code = last_error()
    if code == ERROR_ALREADY_EXISTS:
        return False
    raise BridgeError(
        "PRIVATE_STATE_UNAVAILABLE",
        f"the private state directory could not be created ({winerror(code)})",
    )


def create_private_file(path: pathlib.Path, sddl: str) -> bool:
    """Create one file with ``sddl`` applied at creation time; False if it exists."""
    attributes = security_attributes(sddl)
    handle = KERNEL32.CreateFileW(
        str(path),
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_ALL,
        ctypes.byref(attributes),
        CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle not in (None, INVALID_HANDLE_VALUE):
        KERNEL32.CloseHandle(handle)
        return True
    code = last_error()
    # ERROR_ACCESS_DENIED belongs here: Windows reports it instead of ERROR_FILE_EXISTS when
    # the object is already there and its descriptor denies this creation. That is the
    # pre-planted-state case §25 exists for, so the caller verifies it and refuses it rather
    # than the Bridge treating it as a failure it may repair.
    if code in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED):
        return False
    raise BridgeError(
        "PRIVATE_STATE_UNAVAILABLE",
        f"the private state file could not be created ({winerror(code)})",
    )
