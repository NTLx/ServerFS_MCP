//! Coded, platform-normalized kernel errors.
//!
//! These carry conditions the higher layers can map to the ServerFS
//! agent-facing codes; raw NTSTATUS/DWORD values stay in
//! [`NativeError::Unexpected`] for debug reporting only and must never be
//! rendered into an agent-facing message.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NativeError {
    /// The object (or one intermediate directory) does not exist.
    PathNotFound,
    /// A component that must be a directory is not one.
    NotADirectory,
    /// The target must be a file but is a directory.
    IsADirectory,
    /// The object is a reparse point; traversal refuses to open through it.
    ReparsePoint,
    /// The filesystem refused access for the current process.
    AccessDenied,
    /// A file exceeded a channel byte limit (the tool layer renders the
    /// channel-specific agent code).
    FileTooLarge,
    /// The object's revision changed while it was being read; no mixed or
    /// stale payload may be returned.
    ChangedDuringRead,
    /// One text line exceeded the page byte budget.
    LineTooLarge { line: u64, max_bytes: u64 },
    /// A name is rejected by kernel-level validation before any syscall
    /// (empty, embedded NUL, path separators, `.`/`..`, trailing dots or
    /// spaces, oversized).
    InvalidName,
    /// The root path is not an absolute drive path usable as a trusted
    /// anchor.
    InvalidRoot,
    /// Any other kernel failure, preserved for debug logs only.
    Unexpected {
        /// NTSTATUS (nt calls) when `nt` is true; otherwise a Win32 error.
        code: u32,
        nt: bool,
    },
}

impl NativeError {
    pub(crate) fn nt(status: u32) -> Self {
        // MSDN-documented NTSTATUS values used by NtCreateFile opens.
        const STATUS_OBJECT_NAME_NOT_FOUND: u32 = 0xC000_0034;
        const STATUS_OBJECT_PATH_NOT_FOUND: u32 = 0xC000_003A;
        const STATUS_NO_SUCH_FILE: u32 = 0xC000_000F;
        const STATUS_ACCESS_DENIED: u32 = 0xC000_0022;
        const STATUS_NOT_A_DIRECTORY: u32 = 0xC000_0103;
        const STATUS_FILE_IS_A_DIRECTORY: u32 = 0xC000_00BA;
        const STATUS_BAD_NETWORK_NAME: u32 = 0xC000_00CC;

        match status {
            STATUS_OBJECT_NAME_NOT_FOUND | STATUS_OBJECT_PATH_NOT_FOUND | STATUS_NO_SUCH_FILE => {
                NativeError::PathNotFound
            }
            STATUS_BAD_NETWORK_NAME => NativeError::PathNotFound,
            STATUS_ACCESS_DENIED => NativeError::AccessDenied,
            STATUS_NOT_A_DIRECTORY => NativeError::NotADirectory,
            STATUS_FILE_IS_A_DIRECTORY => NativeError::IsADirectory,
            _ => NativeError::Unexpected {
                code: status,
                nt: true,
            },
        }
    }

    pub(crate) fn win32(code: u32) -> Self {
        const ERROR_FILE_NOT_FOUND: u32 = 2;
        const ERROR_PATH_NOT_FOUND: u32 = 3;
        const ERROR_ACCESS_DENIED: u32 = 5;

        match code {
            ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => NativeError::PathNotFound,
            ERROR_ACCESS_DENIED => NativeError::AccessDenied,
            _ => NativeError::Unexpected { code, nt: false },
        }
    }
}

impl fmt::Display for NativeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NativeError::PathNotFound => f.write_str("path not found"),
            NativeError::NotADirectory => f.write_str("component is not a directory"),
            NativeError::IsADirectory => f.write_str("target is a directory"),
            NativeError::ReparsePoint => f.write_str("reparse point refused"),
            NativeError::AccessDenied => f.write_str("access denied"),
            NativeError::FileTooLarge => f.write_str("file exceeds the byte limit"),
            NativeError::ChangedDuringRead => f.write_str("file changed while it was being read"),
            NativeError::LineTooLarge { line, max_bytes } => {
                write!(f, "line {line} exceeds {max_bytes} bytes")
            }
            NativeError::InvalidName => f.write_str("invalid component name"),
            NativeError::InvalidRoot => f.write_str("invalid workdir root"),
            NativeError::Unexpected { code, nt } => {
                write!(
                    f,
                    "kernel failure ({code:#010x}, {})",
                    if *nt { "ntstatus" } else { "win32" }
                )
            }
        }
    }
}

impl std::error::Error for NativeError {}
