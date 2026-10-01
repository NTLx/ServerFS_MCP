//! Name validation and UTF-16 encoding for relative component opens.
//!
//! The MCP policy layer has already normalized the agent-visible path into
//! `rel_parts` (no `.`/`..`, no separators inside a component). This module
//! is the kernel-side second gate: a component name that reaches the NT
//! open with a separator or a dot segment would re-introduce path parsing
//! into a handle-relative API, so it is refused before any syscall.

use crate::error::NativeError;

/// Validate one already-resolved path component for a relative open.
pub fn validate_component(name: &str) -> Result<(), NativeError> {
    if name.is_empty() || name == "." || name == ".." {
        return Err(NativeError::InvalidName);
    }
    if name.contains('\0') || name.contains('\\') || name.contains('/') || name.contains(':') {
        return Err(NativeError::InvalidName);
    }
    // Windows trims trailing dots and spaces at the object-manager level,
    // so "file." and "file" would name the same object through a different
    // string. Refuse the ambiguity instead of letting the kernel normalize.
    if name.ends_with('.') || name.ends_with(' ') {
        return Err(NativeError::InvalidName);
    }
    // NT component names are bounded by the 255 UTF-16 units on mainstream
    // filesystems; the UNICODE_STRING length field is u16 bytes anyway.
    if name.encode_utf16().count() > 255 {
        return Err(NativeError::InvalidName);
    }
    Ok(())
}

/// An owned UTF-16 buffer usable as an NT `UNICODE_STRING`.
pub struct NtName {
    buffer: Vec<u16>,
}

impl NtName {
    pub fn new(name: &str) -> Result<NtName, NativeError> {
        validate_component(name)?;
        let buffer: Vec<u16> = name.encode_utf16().collect();
        if buffer.len() * 2 > u16::MAX as usize {
            return Err(NativeError::InvalidName);
        }
        Ok(NtName { buffer })
    }

    pub(crate) fn unicode_string(&mut self) -> windows_sys::Win32::Foundation::UNICODE_STRING {
        use windows_sys::Win32::Foundation::UNICODE_STRING;
        let bytes = self.buffer.len() * 2;
        UNICODE_STRING {
            Length: bytes as u16,
            MaximumLength: (self.buffer.capacity() * 2) as u16,
            Buffer: self.buffer.as_mut_ptr(),
        }
    }
}

/// Encode an absolute root path to a NULL-terminated wide string with the
/// `\\?\` prefix, which requests the literal Win32 namespace: no further
/// path parsing, MAX_PATH relaxation, and dots/spaces are not trimmed.
pub fn encoded_root(path: &str) -> Result<Vec<u16>, NativeError> {
    let drive_absolute = path.len() >= 3
        && path.as_bytes()[0].is_ascii_alphabetic()
        && path.as_bytes()[1] == b':'
        && path.as_bytes()[2] == b'\\';
    if !drive_absolute && !path.starts_with("\\\\?\\") {
        // Trusted-anchor roots must be absolute drive paths; UNC and other
        // namespaces are outside the v0.10 Windows scope.
        return Err(NativeError::InvalidRoot);
    }
    if path.contains('\0') {
        return Err(NativeError::InvalidRoot);
    }
    // In the literal namespace nothing is normalized: a trailing backslash
    // on a non-root directory would simply name a nonexistent object.
    let normalized: &str =
        if path.len() > 3 && path.ends_with('\\') && !path[..path.len() - 1].ends_with(':') {
            &path[..path.len() - 1]
        } else {
            path
        };
    let mut wide: Vec<u16> = if normalized.starts_with("\\\\?\\") {
        normalized.encode_utf16().collect()
    } else {
        "\\\\?\\"
            .encode_utf16()
            .chain(normalized.encode_utf16())
            .collect()
    };
    wide.push(0);
    Ok(wide)
}
