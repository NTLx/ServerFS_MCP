//! Directory enumeration through the Windows backend (§16).
//!
//! The scan starts from an already opened/validated directory HANDLE. The
//! batch names from `NtQueryDirectoryFile` are candidates only: every
//! entry that will be reported is re-opened HANDLE-relatively (as itself,
//! never following the link) and its reported type/size/time come from
//! that second, authoritative handle. Entries that vanish or fail to open
//! mid-scan are skipped — exactly the Linux `scandir` behavior — and no
//! failure ever re-resolves a path string.

use crate::error::NativeError;
use crate::ffi::DirectoryScan;
use crate::metadata::{self, NativeMetadata};
use crate::traversal;
use crate::Handle;

pub struct ListedEntry {
    pub name: String,
    pub metadata: NativeMetadata,
}

/// Enumerate one directory handle into report entries (unsorted,
/// unfiltered: policy filtering and pagination stay in the session
/// layer, mirroring the Linux backend split).
pub fn list_directory(dir: &Handle) -> Result<Vec<ListedEntry>, NativeError> {
    let names = candidate_names(dir)?;
    let mut entries = Vec::with_capacity(names.len());
    for name in names {
        // Authoritative reopen. The link itself is opened as an object,
        // never followed: type comes from the reopened handle.
        let Ok(handle) = traversal::open_component_raw(dir, &name) else {
            continue; // vanished or raced mid-scan — skip like Linux
        };
        match metadata::collect(&handle) {
            Ok(md) => entries.push(ListedEntry { name, metadata: md }),
            Err(_) => continue,
        }
    }
    Ok(entries)
}

fn candidate_names(dir: &Handle) -> Result<Vec<String>, NativeError> {
    let mut scan = DirectoryScan::new();
    let mut names = Vec::new();
    while let Some(batch) = scan.next_batch(dir)? {
        names.extend(batch);
    }
    Ok(names)
}
