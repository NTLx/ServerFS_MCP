//! The §13 traversal primitive: HANDLE-relative component opens with
//! reparse refusal and on-handle type validation.
//!
//! Mirrors the Linux `fdio` invariant: the root is opened once by name as
//! a trusted anchor; every request-derived component is then opened
//! relative to an already-verified parent object. Because each open names
//! exactly one component (`OBJECT_ATTRIBUTES.RootDirectory` + bare name),
//! there is no intermediate path string for an attacker — or the object
//! manager — to re-resolve.

use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
};

use crate::error::NativeError;
use crate::ffi::{self, OpenKind};
use crate::handle::Handle;
use crate::path::NtName;

/// Open the workdir root by name (the only name-resolving open allowed)
/// and prove it is a real directory, not a reparse point.
pub fn open_root(path: &str) -> Result<Handle, NativeError> {
    open_root_with_access(path, ffi::DIR_TRAVERSE_ACCESS)
}

/// Open the single trusted root anchor with the exact access needed by the
/// session role. Writable sessions include only the directory add rights
/// used by relative create/publication operations.
pub fn open_root_with_access(path: &str, access: u32) -> Result<Handle, NativeError> {
    let wide = crate::path::encoded_root(path)?;
    let handle = ffi::open_root_by_name(&wide, access)?;
    validate(&handle, OpenKind::Directory)?;
    Ok(handle)
}

/// Open one validated component relative to a trusted parent directory.
pub fn open_component(
    parent: &Handle,
    name: &str,
    expect: OpenKind,
) -> Result<Handle, NativeError> {
    let mut nt_name = NtName::new(name)?;
    let access = match expect {
        OpenKind::Directory => ffi::DIR_TRAVERSE_ACCESS,
        OpenKind::File => ffi::FILE_READ_ACCESS,
        OpenKind::Any => ffi::READ_ATTRIBUTES_ONLY,
    };
    let mut unicode = nt_name.unicode_string();
    let handle = ffi::open_relative(parent, &mut unicode, access, expect)?;
    validate(&handle, expect)?;
    Ok(handle)
}

pub fn open_component_with_access(
    parent: &Handle,
    name: &str,
    expect: OpenKind,
    access: u32,
) -> Result<Handle, NativeError> {
    let mut nt_name = NtName::new(name)?;
    let mut unicode = nt_name.unicode_string();
    let handle = ffi::open_relative(parent, &mut unicode, access, expect)?;
    validate(&handle, expect)?;
    Ok(handle)
}

/// Mutation-kernel variant: same strict validation, but the caller fixes
/// the share mode because held targets harden the mutation window
/// (§19.4). Ordinary traversal never calls this with anything but the
/// full sharing mask.
pub fn open_component_with_share(
    parent: &Handle,
    name: &str,
    expect: OpenKind,
    access: u32,
    share_mode: u32,
) -> Result<Handle, NativeError> {
    let mut nt_name = NtName::new(name)?;
    let mut unicode = nt_name.unicode_string();
    let handle = ffi::open_relative_with_share(parent, &mut unicode, access, expect, share_mode)?;
    validate(&handle, expect)?;
    Ok(handle)
}

/// Open one component relative to a trusted parent WITHOUT the refusal
/// filters: the caller classifies the object from its own handle
/// attributes. Used for report channels (stat/list entries) where a
/// reparse point is a legitimate reported type — and for nothing else.
/// Reads, resolution through intermediates and future mutations must keep
/// using `open_component`/`resolve`, which refuse reparse objects.
pub fn open_component_raw(parent: &Handle, name: &str) -> Result<Handle, NativeError> {
    let mut nt_name = NtName::new(name)?;
    let mut unicode = nt_name.unicode_string();
    ffi::open_relative(
        parent,
        &mut unicode,
        ffi::READ_ATTRIBUTES_ONLY,
        OpenKind::Any,
    )
}

/// Resolve a chain for reporting: every intermediate must be a real
/// non-reparse directory; the final component is opened as itself so the
/// caller can classify it (file/directory/reparse_point).
pub fn resolve_for_report(root: &Handle, components: &[&str]) -> Result<Handle, NativeError> {
    let (last, intermediates) = components.split_last().ok_or(NativeError::InvalidName)?;
    if intermediates.is_empty() {
        return open_component_raw(root, last);
    }
    let current = resolve(root, intermediates, OpenKind::Directory)?;
    open_component_raw(&current, last)
}

/// Resolve a full component chain from the root, opening each component
/// relative to the previous one.
///
/// Retention semantics (§13): the parent directory handle stays open while
/// its child open runs; after the child handle exists it keeps the object
/// referenced on its own, so the parent is released as the walk advances.
/// No open ever resolves a multi-component path string.
pub fn resolve(root: &Handle, components: &[&str], leaf: OpenKind) -> Result<Handle, NativeError> {
    if components.is_empty() {
        return Err(NativeError::InvalidName);
    }
    let mut current = open_component(
        root,
        components[0],
        if components.len() == 1 {
            leaf
        } else {
            OpenKind::Directory
        },
    )?;
    for (index, component) in components.iter().enumerate().skip(1) {
        let expect = if index + 1 == components.len() {
            leaf
        } else {
            OpenKind::Directory
        };
        let next = open_component(&current, component, expect)?;
        current = next;
    }
    Ok(current)
}

/// Post-open validation on the object itself: never a reparse point, and
/// the expected directory/file nature. The attributes are read from the
/// handle the kernel just returned — the same object every later operation
/// will use.
pub fn validate(handle: &Handle, expect: OpenKind) -> Result<(), NativeError> {
    let tag = ffi::attribute_tag(handle)?;
    if tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(NativeError::ReparsePoint);
    }
    let is_dir = tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    match expect {
        OpenKind::Directory if !is_dir => Err(NativeError::NotADirectory),
        OpenKind::File if is_dir => Err(NativeError::IsADirectory),
        _ => Ok(()),
    }
}
