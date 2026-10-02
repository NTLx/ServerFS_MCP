//! Create-only mutation orchestration over retained directory handles.
//!
//! This module contains no unsafe code and never resolves a request path by
//! string. It resolves the parent chain with the traversal kernel, then uses
//! one validated leaf component for create and HANDLE-based publication.

use std::sync::atomic::{AtomicU64, Ordering};

use crate::error::NativeError;
use crate::metadata;
use crate::path::NtName;
use crate::{ffi, traversal, Handle};

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);
const TEMP_ATTEMPTS: usize = 32;

struct TempFile {
    handle: Handle,
    published: bool,
}

impl Drop for TempFile {
    fn drop(&mut self) {
        if !self.published {
            // Cleanup is best effort. Preserve the primary operation error;
            // Windows acceptance checks enumerate the parent for leftovers.
            let _ = ffi::mark_for_delete(&self.handle);
        }
    }
}

fn parent_and_leaf<'a>(
    root: &Handle,
    parts: &'a [&'a str],
) -> Result<(Option<Handle>, &'a str), NativeError> {
    let (leaf, parents) = parts.split_last().ok_or(NativeError::InvalidName)?;
    crate::path::validate_component(leaf)?;
    let parent = if parents.is_empty() {
        None
    } else {
        Some(
            traversal::resolve_create_parent(root, parents).map_err(|err| match err {
                NativeError::PathNotFound => NativeError::ParentNotFound,
                other => other,
            })?,
        )
    };
    Ok((parent, leaf))
}

fn create_temp(parent: &Handle) -> Result<TempFile, NativeError> {
    for _ in 0..TEMP_ATTEMPTS {
        let serial = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!(".serverfs-tmp-{}-{serial:016x}", std::process::id());
        let mut nt_name = NtName::new(&name)?;
        let mut unicode = nt_name.unicode_string();
        match ffi::create_relative(parent, &mut unicode, false) {
            Ok(handle) => {
                return Ok(TempFile {
                    handle,
                    published: false,
                });
            }
            Err(NativeError::AlreadyExists) => continue,
            Err(err) => return Err(err),
        }
    }
    Err(NativeError::ResourceExhausted)
}

fn write_all(handle: &Handle, data: &[u8]) -> Result<(), NativeError> {
    let mut remaining = data;
    while !remaining.is_empty() {
        let written = ffi::write_chunk(handle, remaining)?;
        if written == 0 {
            return Err(NativeError::ResourceExhausted);
        }
        remaining = &remaining[written..];
    }
    Ok(())
}

/// Create a regular file atomically; the final name is never opened before
/// publication, so readers see either no entry or the complete payload.
pub fn create_file(root: &Handle, parts: &[&str], data: &[u8]) -> Result<String, NativeError> {
    let (parent_owned, leaf) = parent_and_leaf(root, parts)?;
    let parent = parent_owned.as_ref().unwrap_or(root);
    let mut temp = create_temp(parent)?;
    write_all(&temp.handle, data)?;
    ffi::flush_file(&temp.handle)?;
    let final_name: Vec<u16> = leaf.encode_utf16().collect();
    if let Err(publication_error) = ffi::rename_no_replace(&temp.handle, parent, &final_name) {
        // Windows filesystems can report a directory/reparse collision with
        // a generic access error. Re-open the single leaf relative to the
        // already-held parent only to normalize the result; publication is
        // still the atomic no-replace HANDLE rename above.
        if traversal::open_component_raw(parent, leaf).is_ok() {
            return Err(NativeError::AlreadyExists);
        }
        if matches!(
            &publication_error,
            NativeError::Unexpected {
                code: 32, // ERROR_SHARING_VIOLATION: occupied destination is held open
                nt: false
            }
        ) {
            return Err(NativeError::AlreadyExists);
        }
        return Err(publication_error);
    }
    temp.published = true;
    metadata::revision_of(&temp.handle)
}

/// Create one final directory relative to a retained parent HANDLE.
pub fn create_directory(root: &Handle, parts: &[&str]) -> Result<String, NativeError> {
    let (parent_owned, leaf) = parent_and_leaf(root, parts)?;
    let parent = parent_owned.as_ref().unwrap_or(root);
    let mut nt_name = NtName::new(leaf)?;
    let mut unicode = nt_name.unicode_string();
    let directory = ffi::create_relative(parent, &mut unicode, true)?;
    metadata::revision_of(&directory)
}
