"""Per-user ownership of the native Agent lifecycle (v0.11 Phase F).

Two measured problems are the same lifecycle-ownership gap, and this module closes it.

**Cross-attachment.** The Agent Bridge endpoint is derived from the user SID alone
(``derive_pipe_name``), so a second Agent-enabled chain in one user session does not get a Bridge of
its own -- its client attaches to the first chain's Bridge and every tool call still succeeds while
the results belong to the other chain. Measured in F3: the second chain had zero Bridges of its own
and its task, artifact and store row all landed in the first chain.

**Unprovable containment.** A crash leaves a recovery guard that startup reconciliation may only
clear when the provider is known to be gone, and no adapter can know that the OS killed it. The
supervisor can, and the proof has two halves that live in two kernel objects: this lease, whose
acquisition shows no earlier owner survives, and the *named* Job Object (``job_name``), whose
creation is only possible once the previous generation's containment object no longer exists.

The two are deliberately different statements. The lease rules out a live competitor; the Job name
rules out a *stale containment object*, which is what actually held the provider tree. Reading
containment off the lease alone would be inferring it from an object that never contained anything.

Scope is deliberately the **user**, matching the Named Pipe, and the artifact lives outside the data
home: a per-data-home scope would let two data homes each claim ownership of the one endpoint they
share. The mechanism is the one already measured in Phase 0A -- an exclusive, non-blocking
``LockFileEx`` over a real artifact -- with its own identity rather than the workdir lease's,
because this is a supervisor lifecycle lease and not a workspace lease.

Because the artifact is now the *proof* the recovery path reasons from, its filesystem identity is
validated to the same standard as the workdir lease: a plain file, in a plain directory. A reparse
point is a redirect -- the lock would land on an object other than the one the next owner validates
-- and this file decides whether a workdir's provider state is treated as recoverable.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import os
from pathlib import Path

from .native_endpoint import derive_pipe_name

_ARTIFACT_SUFFIX = ".lifecycle"

#: The two attributes that make an object unusable as the lock artifact, the values the workdir
#: lease already refuses on (§5.5 item 4). ``FILE_INFO_BY_HANDLE_CLASS`` is zero-based, so
#: ``FileAttributeTagInfo`` (9 in the header) is 9 here.
_FILE_ATTRIBUTE_DIRECTORY = 0x0000_0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0000_0400
_INVALID_FILE_ATTRIBUTES = 0xFFFF_FFFF
_FILE_ATTRIBUTE_TAG_INFO = 9


class LifecycleOwnershipError(Exception):
    """This supervisor may not own the native Agent lifecycle. Redacted by construction."""


def lifecycle_dir() -> Path:
    """Where the per-user lifecycle artifact lives. Not configurable, for the same reason.

    Not under ``SERVERFS_DATA_HOME``: two data homes in one user session share one Named Pipe, so a
    data-home-scoped lease would hand both of them ownership of the same endpoint.

    A settable location is a settable *scope*, and that is the whole defect this lease exists to
    close. So on Windows the root comes from the shell's own knowledge of this token
    (``SHGetKnownFolderPath``), never from an inherited variable: ``LOCALAPPDATA`` is settable by a
    parent, and reading it would let two processes of one user take two locks while sharing the one
    SID-derived pipe -- the same defect by a different name. Measured: the shell API returns the
    same folder today and does not follow a poisoned variable.

    POSIX keeps the platform's own state directory, including its XDG variable. The Agent lifecycle
    is Windows-only (the supervisor, the Job Object and the pipe are all Windows), so this branch
    exists for importability and tests rather than for a supported deployment, and it follows the
    convention a POSIX program is expected to follow.

    A test that needs a private artifact passes ``LifecycleLease(root=...)``.
    """
    if os.name == "nt":
        return window_local_app_data() / "ServerFS" / "agent-lifecycle"
    state = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
    return Path(state) / "serverfs" / "agent-lifecycle"


def is_redirected(attributes: int, reparse_tag: int = 0) -> bool:
    """Whether an object's attributes describe something other than a plain file.

    A directory cannot carry a byte-range lock at all, and a reparse point is a redirect: the lock
    would be taken on an object other than the one the next owner validates. Since this artifact is
    the containment proof, either shape has to fail closed rather than be locked and believed.
    """
    if attributes == _INVALID_FILE_ATTRIBUTES:
        return True
    flagged = attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT)
    return bool(flagged) or bool(reparse_tag)


def is_unusable_directory(attributes: int) -> bool:
    """Whether the lifecycle directory is absent, not a directory, or a redirect."""
    if attributes == _INVALID_FILE_ATTRIBUTES:
        return True
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return not attributes & _FILE_ATTRIBUTE_DIRECTORY


def lifecycle_identity() -> str:
    """The scope identity, spelled exactly as the Named Pipe is: one identity, two uses."""
    if os.name == "nt":
        return derive_pipe_name(_current_user_sid())
    return f"lifecycle-{os.getuid()}"


def lifecycle_artifact_name() -> str:
    """A filesystem-safe artifact name carrying the pipe's identity.

    The pipe name is namespace-qualified and therefore not a legal file name -- measured by the
    first attempt, which failed with ERROR_FILE_NOT_FOUND on a name that looked perfectly reasonable
    until it reached the filesystem. Hashing it keeps the identity exactly the pipe's while making
    the digest the file name.
    """
    return f"serverfs-agent-lifecycle-{_identity_digest()}{_ARTIFACT_SUFFIX}"


def job_name() -> str:
    """The kernel name of the containment Job Object, on the same identity as the pipe.

    Named, and derived from the same digest as the artifact, because the name is what upgrades
    containment from an inference to a reading. An anonymous job cannot answer "did a previous
    generation exist?"; a *named* one can: a job object is destroyed once its last handle closes,
    and closing it is what delivers kill-on-close to every member, so a name that is free means the
    previous containment object is gone and its members have been ordered to stop.
    ``Global\\`` rather than the default session namespace, because this must agree with a
    pipe that is reachable from more than one session while the lease is scoped to the user.

    The claim is deliberately "ordered to stop", not "finished dying": measured on this host, the
    name frees ~0.3-1.5 ms before a member's process object signals exit, because Windows
    termination is asynchronous. The recovery decision needs the first of those, not the second --
    and it is consumed long after it is produced anyway, once the Bridge has spawned, passed
    readiness and started its stdio child.

    Not a path: this is a kernel object name, and it is deliberately not the artifact's spelling.
    """
    return f"Global\\serverfs-agent-bridge-job-v1-{_identity_digest()}"


def _identity_digest() -> str:
    """The one identity both of the above are spelled from, so they cannot disagree on the owner."""
    return hashlib.sha256(lifecycle_identity().encode("utf-8")).hexdigest()[:32]


def _current_user_sid() -> str:
    from .native_endpoint import current_user_sid

    return current_user_sid()


if os.name == "nt":  # pragma: no cover - exercised on the Windows acceptance host
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)
    _OLE32 = ctypes.WinDLL("ole32", use_last_error=True)

    #: ``FOLDERID_LocalAppData``: the per-user local application data folder, by GUID rather than by
    #: name, so the answer comes from the shell's record of *this token* rather than from a string
    #: an ancestor process could have supplied.
    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    _FOLDERID_LOCAL_APPDATA = _GUID(
        Data1=0xF1B32785,
        Data2=0x6FBA,
        Data3=0x4FCF,
        Data4=(ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )

    _SHELL32.SHGetKnownFolderPath.restype = ctypes.c_long
    _SHELL32.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _OLE32.CoTaskMemFree.restype = None
    _OLE32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    def window_local_app_data() -> Path:
        """The canonical per-user local application data folder, from the shell.

        ``SHGetKnownFolderPath`` resolves the folder for the calling token. That is what makes the
        claim true: unlike ``LOCALAPPDATA``, which is just an inherited string, this cannot be
        pointed elsewhere by whoever started the process. Measured: with the variable poisoned,
        the returned folder is unchanged.

        The buffer belongs to the shell and is released with ``CoTaskMemFree`` on both paths.
        """
        buffer = ctypes.c_void_p()
        result = _SHELL32.SHGetKnownFolderPath(
            ctypes.byref(_FOLDERID_LOCAL_APPDATA), 0, None, ctypes.byref(buffer)
        )
        if result != 0 or not buffer.value:
            raise LifecycleOwnershipError(
                "the per-user local application data folder is unavailable"
            )
        try:
            return Path(ctypes.wstring_at(buffer.value))
        finally:
            _OLE32.CoTaskMemFree(buffer)

    _GENERIC_READ = 0x8000_0000
    _GENERIC_WRITE = 0x4000_0000
    _FILE_SHARE_ALL = 0x0000_0007
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x0000_0080
    _LOCKFILE_EXCLUSIVE_LOCK = 0x0000_0002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x0000_0001
    _FULL_RANGE = 0xFFFF_FFFF
    _ERROR_LOCK_VIOLATION = 33
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32.GetFileAttributesW.restype = wintypes.DWORD
    _KERNEL32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    def _overlapped() -> _Overlapped:
        """A fresh zero-offset OVERLAPPED.

        ``LockFileEx`` reads ``Offset`` even on a handle opened without ``FILE_FLAG_OVERLAPPED`` and
        a NULL pointer faults on this OS build -- measured while implementing §5.5.
        """
        return _Overlapped()


def _acquire_handle(path: Path) -> int:
    """Create-if-absent and lock the artifact, or fail closed. Returns the held handle.

    Every refusal here is a ``LifecycleOwnershipError``, including the filesystem ones: the
    supervisor's startup handler knows that type, and a raw ``OSError`` would reach the operator
    outside it.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LifecycleOwnershipError(
            f"the Agent lifecycle directory could not be created ({type(exc).__name__})"
        ) from exc

    if os.name == "nt":  # pragma: no cover - Windows acceptance host
        attributes = _KERNEL32.GetFileAttributesW(str(path.parent))
        if is_unusable_directory(attributes):
            raise LifecycleOwnershipError(
                "the Agent lifecycle directory is not a regular directory"
            )
        handle = _KERNEL32.CreateFileW(
            str(path),
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_ALL,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise LifecycleOwnershipError("the Agent lifecycle lease is not accessible")
        # Validated on the handle that will hold the lock, not by a second path lookup: a path can
        # be swapped between the two, and the artifact is what the containment proof is read from.
        info = _AttributeTagInfo()
        if not _KERNEL32.GetFileInformationByHandleEx(
            handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
        ) or is_redirected(info.FileAttributes, info.ReparseTag):
            _KERNEL32.CloseHandle(handle)
            raise LifecycleOwnershipError("the Agent lifecycle lease is not a regular file")
        locked = _KERNEL32.LockFileEx(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
            0,
            _FULL_RANGE,
            _FULL_RANGE,
            ctypes.byref(_overlapped()),
        )
        if locked:
            return handle
        code = ctypes.get_last_error()
        _KERNEL32.CloseHandle(handle)
        if code == _ERROR_LOCK_VIOLATION:
            raise LifecycleOwnershipError(
                "another Agent-enabled supervisor of this user already owns the lifecycle"
            )
        raise LifecycleOwnershipError("the Agent lifecycle lease is unavailable")

    import fcntl
    import stat as stat_module

    try:
        entry = path.parent.lstat()
    except OSError as exc:
        raise LifecycleOwnershipError(
            f"the Agent lifecycle directory is not accessible ({type(exc).__name__})"
        ) from exc
    if stat_module.S_ISLNK(entry.st_mode) or not stat_module.S_ISDIR(entry.st_mode):
        raise LifecycleOwnershipError("the Agent lifecycle directory is not a regular directory")

    try:
        # O_NOFOLLOW is what makes the artifact check atomic with the open: a symlink planted at the
        # artifact path fails the open instead of being followed and locked.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise LifecycleOwnershipError(
            f"the Agent lifecycle lease is not accessible ({type(exc).__name__})"
        ) from exc
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise LifecycleOwnershipError("the Agent lifecycle lease is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LifecycleOwnershipError(
                "another Agent-enabled supervisor of this user already owns the lifecycle"
            ) from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_handle(handle: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows acceptance host
        _KERNEL32.CloseHandle(handle)
        return
    os.close(handle)


class LifecycleLease:
    """An acquired per-user lifecycle ownership.

    Releases on ``close()`` and on process death: the OS reclaims a file lock when the owning
    process ends, including under ``TerminateProcess``, so a crashed supervisor cannot wedge the
    user's lifecycle permanently.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        base = Path(root) if root is not None else lifecycle_dir()
        self.path = base / lifecycle_artifact_name()
        self._handle: int | None = None

    def acquire(self) -> LifecycleLease:
        self._handle = _acquire_handle(self.path)
        return self

    def close(self) -> None:
        if self._handle is not None:
            _release_handle(self._handle)
            self._handle = None

    def __enter__(self) -> LifecycleLease:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "LifecycleLease",
    "LifecycleOwnershipError",
    "is_redirected",
    "is_unusable_directory",
    "job_name",
    "lifecycle_artifact_name",
    "lifecycle_dir",
]
