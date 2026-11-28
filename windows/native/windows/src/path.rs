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

/// Encode an absolute root path to a NULL-terminated wide string in the
/// `\\?\` literal namespace.
///
/// Accepted shapes are exactly `X:\...` and `\\?\X:\...` — local drive
/// paths only. Everything else is refused without normalization, because
/// the literal namespace performs no string repair and this kernel must
/// open the name the administrator actually configured:
/// UNC (`\\server\share`, `\\?\UNC\...`), the global object namespace
/// (`\\?\GLOBALROOT\...`), device paths (`\\.\...`, `\\?\.\...`) and
/// volume-GUID namespaces are all outside the v0.10 Windows support
/// matrix (local NTFS workdir). A trailing backslash is likewise refused
/// except for the bare volume root (`C:\`, `\\?\C:\`).
pub fn encoded_root(path: &str) -> Result<Vec<u16>, NativeError> {
    if path.contains('\0') {
        return Err(NativeError::InvalidRoot);
    }
    let (prefixed, body) = match path.strip_prefix("\\\\?\\") {
        Some(rest) => (true, rest),
        None => (false, path),
    };
    if prefixed {
        // After the prefix the body must itself be a plain drive path:
        // block the extended special namespaces before any parsing.
        let upper = body.to_ascii_uppercase();
        if upper.starts_with("UNC\\") || upper.starts_with("GLOBALROOT\\") || upper.starts_with('.')
        {
            return Err(NativeError::InvalidRoot);
        }
    }
    if !is_drive_absolute(body) {
        return Err(NativeError::InvalidRoot);
    }
    // Trailing separator: the only legal name ending in `\` is the volume
    // root itself, whose body is exactly "X:\".
    if body.ends_with('\\') && body.len() != 3 {
        return Err(NativeError::InvalidRoot);
    }
    let mut wide: Vec<u16> = if prefixed {
        path.encode_utf16().collect()
    } else {
        "\\\\?\\"
            .encode_utf16()
            .chain(path.encode_utf16())
            .collect()
    };
    wide.push(0);
    Ok(wide)
}

fn is_drive_absolute(body: &str) -> bool {
    let b = body.as_bytes();
    b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b[2] == b'\\'
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_accepted(input: &str, expected_literal: &str) {
        let wide = encoded_root(input).unwrap_or_else(|e| panic!("{input:?} refused: {e}"));
        let got: String = String::from_utf16(&wide[..wide.len() - 1]).unwrap();
        assert_eq!(got, expected_literal, "encoding of {input:?}");
        assert_eq!(*wide.last().unwrap(), 0, "must be NUL-terminated");
    }

    #[test]
    fn root_namespace_accepts_only_local_drive_paths() {
        assert_accepted("C:\\Projects", "\\\\?\\C:\\Projects");
        assert_accepted("d:\\", "\\\\?\\d:\\");
        assert_accepted("\\\\?\\C:\\Projects", "\\\\?\\C:\\Projects");
        assert_accepted("\\\\?\\E:\\", "\\\\?\\E:\\");
    }

    #[test]
    fn root_namespace_refuses_everything_else() {
        for rejected in [
            // trailing separator on a non-root volume path: no silent fix
            "C:\\Projects\\",
            "\\\\?\\C:\\Projects\\",
            "C:\\a\\b\\",
            // UNC and extended special namespaces
            "\\\\server\\share",
            "\\\\?\\UNC\\server\\share",
            "\\\\?\\unc\\server\\share",
            "\\\\?\\GLOBALROOT\\Device\\Harddisk0",
            "\\\\?\\globalroot\\x",
            "\\\\.\\PhysicalDrive0",
            "\\\\?\\.\\PhysicalDrive0",
            "\\\\?\\Volume{60392b61-0000-0000-0000-100000000000}\\",
            // not drive-absolute at all
            "relative\\path",
            "C:/forward",
            "C:",
            "1:\\digit",
            "",
            "\\\\",
            // embedded NUL
            "C:\\Proj\0ects",
        ] {
            assert_eq!(
                encoded_root(rejected),
                Err(NativeError::InvalidRoot),
                "must refuse {rejected:?}"
            );
        }
    }

    #[test]
    fn component_gate_rejects_ambiguous_and_structured_names() {
        for bad in [
            "",
            ".",
            "..",
            "a\\b",
            "a/b",
            "a:b",
            "a\0b",
            "x.",
            "x ",
            "y".repeat(300).as_str(),
        ] {
            assert_eq!(
                validate_component(bad),
                Err(NativeError::InvalidName),
                "{bad:?}"
            );
        }
        for good in [
            "a",
            "..a",
            ".hidden",
            "name with space",
            "x",
            "文件",
            "emoji-📁",
        ] {
            assert_eq!(validate_component(good), Ok(()), "{good:?}");
        }
    }
}
