//! Handle-relative Windows mutation primitives.
//!
//! Replacement policy is intentionally narrow. Ordinary DOS attributes and
//! basic timestamps plus the DACL are copied; owner/group must naturally
//! match on the temporary file. Named streams, EAs,
//! object IDs, multiple hard links, reparses, sparse/compressed/encrypted,
//! offline/cloud, integrity and unknown attribute state are refused. Audit
//! SACLs are outside the v0.10 preservation set; no SACL privilege is asked
//! for and no SACL preservation claim is made.
//!
//! Public revisions keep the Phase B contract. Replacement additionally
//! compares a private fingerprint of its preservation snapshot at the final
//! name-relative gate. This narrows metadata-only races without pretending
//! to provide compare-and-swap against non-cooperating writers: one such
//! writer can still race between that gate and the atomic relative rename.

use sha2::{Digest, Sha256};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_ARCHIVE, FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_NORMAL,
    FILE_ATTRIBUTE_NOT_CONTENT_INDEXED, FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_SYSTEM,
    FILE_ATTRIBUTE_TEMPORARY, FILE_BASIC_INFO,
};

use crate::error::NativeError;
use crate::{ffi, metadata, traversal, Handle};

const TEMP_ATTEMPTS: usize = 16;
const WRITE_CHUNK: usize = 64 * 1024;
pub const INTERNAL_TEMP_PREFIX: &str = ".serverfs-tmp-";
const SAFE_BASIC_ATTRIBUTES: u32 = FILE_ATTRIBUTE_READONLY
    | FILE_ATTRIBUTE_HIDDEN
    | FILE_ATTRIBUTE_SYSTEM
    | FILE_ATTRIBUTE_ARCHIVE
    | FILE_ATTRIBUTE_NORMAL
    | FILE_ATTRIBUTE_TEMPORARY
    | FILE_ATTRIBUTE_NOT_CONTENT_INDEXED;
const REPLACEMENT_ACCESS: u32 = ffi::FILE_READ_ACCESS | ffi::FILE_READ_ATTRIBUTES_ACCESS;
const DELETE_ACCESS: u32 =
    ffi::FILE_READ_ATTRIBUTES_ACCESS | ffi::FILE_DELETE_ACCESS | ffi::SYNCHRONIZE_ACCESS;
const DIRECTORY_DELETE_ACCESS: u32 = DELETE_ACCESS | 0x0000_0001;
const TEMP_ACCESS: u32 = ffi::FILE_WRITE_DATA_ACCESS
    | ffi::FILE_READ_ATTRIBUTES_ACCESS
    | ffi::FILE_WRITE_ATTRIBUTES_ACCESS
    | ffi::FILE_DELETE_ACCESS
    | ffi::READ_CONTROL_ACCESS
    | ffi::WRITE_DAC_ACCESS
    | ffi::SYNCHRONIZE_ACCESS;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct UnsupportedState {
    named_streams: bool,
    ea_data: bool,
    object_id: bool,
    special_attributes: u32,
}

#[derive(Clone)]
struct PreservationSnapshot {
    id: metadata::ObjectId,
    basic: FILE_BASIC_INFO,
    security_descriptor: ffi::SecurityDescriptor,
    owner_sid: Vec<u8>,
    group_sid: Vec<u8>,
    unsupported: UnsupportedState,
}

impl PreservationSnapshot {
    fn fingerprint(&self) -> [u8; 32] {
        let mut digest = Sha256::new();
        digest.update(self.id.volume_serial.to_le_bytes());
        digest.update(self.id.file_id.to_le_bytes());
        digest.update(self.basic.CreationTime.to_le_bytes());
        digest.update(self.basic.LastAccessTime.to_le_bytes());
        digest.update(self.basic.LastWriteTime.to_le_bytes());
        digest.update(self.basic.ChangeTime.to_le_bytes());
        digest.update(self.basic.FileAttributes.to_le_bytes());
        digest.update((self.security_descriptor.bytes().len() as u64).to_le_bytes());
        digest.update(self.security_descriptor.bytes());
        digest.update([
            self.unsupported.named_streams as u8,
            self.unsupported.ea_data as u8,
            self.unsupported.object_id as u8,
        ]);
        digest.update(self.unsupported.special_attributes.to_le_bytes());
        digest.finalize().into()
    }

    fn has_unsupported_state(&self) -> bool {
        self.unsupported.named_streams
            || self.unsupported.ea_data
            || self.unsupported.object_id
            || self.unsupported.special_attributes != 0
    }
}

fn resolve_parent(root: &Handle, parts: &[&str]) -> Result<Option<Handle>, NativeError> {
    if parts.is_empty() {
        return Err(NativeError::RootMutationRefused);
    }
    let parent_parts = &parts[..parts.len() - 1];
    if parent_parts.is_empty() {
        return Ok(None);
    }
    let mut anchor: Option<Handle> = None;
    for component in &parent_parts[..parent_parts.len() - 1] {
        let parent = anchor.as_ref().unwrap_or(root);
        anchor = Some(traversal::open_component(
            parent,
            component,
            crate::ffi::OpenKind::Directory,
        )?);
    }
    let parent = anchor.as_ref().unwrap_or(root);
    let final_parent = traversal::open_component_with_access(
        parent,
        parent_parts[parent_parts.len() - 1],
        crate::ffi::OpenKind::Directory,
        ffi::DIR_MUTATION_ACCESS,
    )?;
    Ok(Some(final_parent))
}

fn parent_handle<'a>(root: &'a Handle, owned: &'a Option<Handle>) -> &'a Handle {
    owned.as_ref().unwrap_or(root)
}

fn target_name<'a>(parts: &[&'a str]) -> Result<&'a str, NativeError> {
    let name = parts
        .last()
        .copied()
        .ok_or(NativeError::RootMutationRefused)?;
    crate::path::validate_component(name)?;
    Ok(name)
}

fn open_leaf(
    parent: &Handle,
    name: &str,
    access: u32,
    kind: ffi::OpenKind,
) -> Result<Handle, NativeError> {
    let mut nt_name = crate::path::NtName::new(name)?;
    let mut unicode = nt_name.unicode_string();
    let handle = ffi::open_relative(parent, &mut unicode, access, kind)?;
    traversal::validate(&handle, kind)?;
    Ok(handle)
}

fn capture_snapshot(handle: &Handle) -> Result<PreservationSnapshot, NativeError> {
    let md = metadata::collect(handle)?;
    if md.is_reparse {
        return Err(NativeError::ReparsePoint);
    }
    if md.is_directory {
        return Err(NativeError::IsADirectory);
    }
    if md.link_count > 1 {
        return Err(NativeError::MultipleHardlinksNotSupported);
    }
    let basic = ffi::basic_info(handle)?;
    let unsupported = UnsupportedState {
        named_streams: ffi::has_named_streams(handle)?,
        ea_data: ffi::has_ea_data(handle)?,
        object_id: ffi::has_object_id(handle)?,
        special_attributes: basic.FileAttributes & !SAFE_BASIC_ATTRIBUTES,
    };
    let security_descriptor = ffi::security_descriptor(handle)?;
    let (owner_sid, group_sid) = security_descriptor.owner_group()?;
    Ok(PreservationSnapshot {
        id: md.id,
        basic,
        security_descriptor,
        owner_sid,
        group_sid,
        unsupported,
    })
}

struct TempFile {
    handle: Handle,
    published: bool,
}

impl Drop for TempFile {
    fn drop(&mut self) {
        if !self.published {
            let _ = ffi::mark_delete(&self.handle);
        }
    }
}

fn create_temp(parent: &Handle) -> Result<(TempFile, String), NativeError> {
    for _ in 0..TEMP_ATTEMPTS {
        let mut random = [0u8; 16];
        ffi::random_bytes(&mut random)?;
        let name = format!("{INTERNAL_TEMP_PREFIX}{}.tmp", hex(&random));
        let mut nt_name = crate::path::NtName::new(&name)?;
        let mut unicode = nt_name.unicode_string();
        match ffi::create_relative(
            parent,
            &mut unicode,
            TEMP_ACCESS,
            false,
            FILE_ATTRIBUTE_NORMAL,
        ) {
            Ok(handle) => {
                return Ok((
                    TempFile {
                        handle,
                        published: false,
                    },
                    name,
                ));
            }
            Err(NativeError::PathAlreadyExists) => continue,
            Err(error) => return Err(error),
        }
    }
    Err(NativeError::MutationIoError)
}

fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(out, "{byte:02x}");
    }
    out
}

fn write_and_flush(handle: &Handle, bytes: &[u8]) -> Result<(), NativeError> {
    for chunk in bytes.chunks(WRITE_CHUNK) {
        let written = ffi::write_chunk(handle, chunk).map_err(|_| NativeError::MutationIoError)?;
        if written != chunk.len() {
            return Err(NativeError::MutationIoError);
        }
    }
    ffi::flush_file(handle).map_err(|_| NativeError::MutationIoError)
}

fn apply_snapshot(temp: &Handle, snapshot: &PreservationSnapshot) -> Result<(), NativeError> {
    let temp_security = ffi::security_descriptor(temp)?;
    let (temp_owner, temp_group) = temp_security.owner_group()?;
    if temp_owner != snapshot.owner_sid || temp_group != snapshot.group_sid {
        return Err(NativeError::MetadataPreservationFailed);
    }
    ffi::apply_dacl(temp, &snapshot.security_descriptor)?;
    if ffi::security_descriptor(temp)? != snapshot.security_descriptor {
        return Err(NativeError::MetadataPreservationFailed);
    }
    let info = FILE_BASIC_INFO {
        CreationTime: snapshot.basic.CreationTime,
        LastAccessTime: snapshot.basic.LastAccessTime,
        LastWriteTime: snapshot.basic.LastWriteTime,
        // NTFS owns ChangeTime and may update it as metadata is applied.
        ChangeTime: 0,
        FileAttributes: snapshot.basic.FileAttributes,
    };
    ffi::set_basic(temp, &info)
}

fn check_expected(
    handle: &Handle,
    expected: &str,
) -> Result<metadata::NativeMetadata, NativeError> {
    let md = metadata::collect(handle)?;
    if md.revision() != expected {
        return Err(NativeError::RevisionConflict);
    }
    Ok(md)
}

pub fn create_bytes(root: &Handle, parts: &[&str], bytes: &[u8]) -> Result<String, NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let (mut temp, _) = create_temp(parent)?;
    write_and_flush(&temp.handle, bytes)?;
    ffi::rename_relative(&temp.handle, parent, name, false)?;
    temp.published = true;
    metadata::revision_of(&temp.handle)
}

pub fn replace_bytes(
    root: &Handle,
    parts: &[&str],
    bytes: &[u8],
    expected_revision: &str,
) -> Result<String, NativeError> {
    replace_bytes_transaction(root, parts, bytes, expected_revision, || {})
}

#[cfg(test)]
fn replace_bytes_with_hook(
    root: &Handle,
    parts: &[&str],
    bytes: &[u8],
    expected_revision: &str,
    before_gate: impl FnOnce(),
) -> Result<String, NativeError> {
    replace_bytes_transaction(root, parts, bytes, expected_revision, before_gate)
}

fn replace_bytes_transaction(
    root: &Handle,
    parts: &[&str],
    bytes: &[u8],
    expected_revision: &str,
    before_gate: impl FnOnce(),
) -> Result<String, NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let original = open_leaf(parent, name, REPLACEMENT_ACCESS, ffi::OpenKind::File)?;
    let initial_md = check_expected(&original, expected_revision)?;
    let snapshot = capture_snapshot(&original)?;
    if snapshot.id != initial_md.id || snapshot.has_unsupported_state() {
        return Err(NativeError::MetadataPreservationFailed);
    }
    let initial_fingerprint = snapshot.fingerprint();
    let (mut temp, _) = create_temp(parent)?;
    write_and_flush(&temp.handle, bytes)?;
    apply_snapshot(&temp.handle, &snapshot)?;
    ffi::flush_file(&temp.handle).map_err(|_| NativeError::MutationIoError)?;

    before_gate();
    let final_target = match open_leaf(parent, name, REPLACEMENT_ACCESS, ffi::OpenKind::File) {
        Ok(handle) => handle,
        Err(NativeError::PathNotFound) => return Err(NativeError::PathNotFound),
        Err(_) => return Err(NativeError::RevisionConflict),
    };
    let final_md = metadata::collect(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    let final_snapshot =
        capture_snapshot(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.id != initial_md.id
        || final_md.revision() != expected_revision
        || final_snapshot.fingerprint() != initial_fingerprint
        || final_snapshot.unsupported != snapshot.unsupported
    {
        return Err(NativeError::RevisionConflict);
    }
    ffi::rename_relative(&temp.handle, parent, name, true)?;
    temp.published = true;
    metadata::revision_of(&temp.handle)
}

pub fn delete_file(
    root: &Handle,
    parts: &[&str],
    expected_revision: &str,
) -> Result<(), NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let original = open_leaf(parent, name, DELETE_ACCESS, ffi::OpenKind::File)?;
    let initial = check_expected(&original, expected_revision)?;
    let final_target =
        open_leaf(parent, name, DELETE_ACCESS, ffi::OpenKind::File).map_err(|error| {
            if error == NativeError::PathNotFound {
                error
            } else {
                NativeError::RevisionConflict
            }
        })?;
    let final_md = metadata::collect(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.id != initial.id || final_md.revision() != expected_revision {
        return Err(NativeError::RevisionConflict);
    }
    // Deletion applies to the verified object handle. If a non-cooperating
    // writer renames it after the final gate, this removes that object at
    // its new name rather than deleting an unverified replacement.
    ffi::mark_delete(&original)
}

pub fn create_directory(root: &Handle, parts: &[&str]) -> Result<String, NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let mut nt_name = crate::path::NtName::new(name)?;
    let mut unicode = nt_name.unicode_string();
    let directory = ffi::create_relative(
        parent,
        &mut unicode,
        DIRECTORY_DELETE_ACCESS,
        true,
        FILE_ATTRIBUTE_NORMAL,
    )?;
    metadata::revision_of(&directory)
}

fn is_empty(directory: &Handle) -> Result<bool, NativeError> {
    let mut scan = ffi::DirectoryScan::new();
    loop {
        match scan.next_batch(directory)? {
            Some(names) if names.is_empty() => continue,
            Some(_) => return Ok(false),
            None => return Ok(true),
        }
    }
}

pub fn delete_directory(
    root: &Handle,
    parts: &[&str],
    expected_revision: &str,
) -> Result<(), NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let original = open_leaf(
        parent,
        name,
        DIRECTORY_DELETE_ACCESS,
        ffi::OpenKind::Directory,
    )?;
    let initial = check_expected(&original, expected_revision)?;
    if !is_empty(&original)? {
        return Err(NativeError::DirectoryNotEmpty);
    }
    let final_target = open_leaf(
        parent,
        name,
        DIRECTORY_DELETE_ACCESS,
        ffi::OpenKind::Directory,
    )
    .map_err(|error| {
        if error == NativeError::PathNotFound {
            error
        } else {
            NativeError::RevisionConflict
        }
    })?;
    let final_md = metadata::collect(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.id != initial.id || final_md.revision() != expected_revision {
        return Err(NativeError::RevisionConflict);
    }
    if !is_empty(&final_target).map_err(|_| NativeError::RevisionConflict)? {
        return Err(NativeError::DirectoryNotEmpty);
    }
    ffi::mark_delete(&original).map_err(|error| {
        if error == NativeError::DirectoryNotEmpty {
            NativeError::DirectoryNotEmpty
        } else {
            error
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows_sys::Win32::Security::{
        InitializeSecurityDescriptor, SetKernelObjectSecurity, SetSecurityDescriptorDacl,
        DACL_SECURITY_INFORMATION,
    };

    struct TestRoot(std::path::PathBuf);

    impl TestRoot {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "serverfs_mutation_unit_{}_{}_{label}",
                std::process::id(),
                TEMP_TEST_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn open(&self) -> Handle {
            traversal::open_root_with_access(self.0.to_str().unwrap(), ffi::DIR_MUTATION_ACCESS)
                .unwrap()
        }
    }

    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    static TEMP_TEST_COUNTER: std::sync::atomic::AtomicUsize =
        std::sync::atomic::AtomicUsize::new(0);

    fn set_null_dacl(root: &Handle, name: &str) {
        let mut nt_name = crate::path::NtName::new(name).unwrap();
        let mut unicode = nt_name.unicode_string();
        let target = ffi::open_relative(
            root,
            &mut unicode,
            ffi::WRITE_DAC_ACCESS | ffi::SYNCHRONIZE_ACCESS,
            ffi::OpenKind::File,
        )
        .unwrap();
        let mut descriptor = windows_sys::Win32::Security::SECURITY_DESCRIPTOR::default();
        assert_ne!(
            unsafe {
                InitializeSecurityDescriptor(
                    (&mut descriptor as *mut windows_sys::Win32::Security::SECURITY_DESCRIPTOR)
                        .cast(),
                    1,
                )
            },
            0
        );
        assert_ne!(
            unsafe {
                SetSecurityDescriptorDacl(
                    (&mut descriptor as *mut windows_sys::Win32::Security::SECURITY_DESCRIPTOR)
                        .cast(),
                    1,
                    std::ptr::null(),
                    0,
                )
            },
            0
        );
        assert_ne!(
            unsafe {
                SetKernelObjectSecurity(
                    target.as_raw(),
                    DACL_SECURITY_INFORMATION,
                    (&mut descriptor as *mut windows_sys::Win32::Security::SECURITY_DESCRIPTOR)
                        .cast(),
                )
            },
            0
        );
    }

    #[test]
    fn private_fingerprint_detects_dacl_drift_without_public_revision_change() {
        let sandbox = TestRoot::new("dacl_drift");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"old bytes").unwrap();
        let error = replace_bytes_with_hook(&root, &["target"], b"new bytes", &expected, || {
            set_null_dacl(&root, "target");
        })
        .unwrap_err();
        assert_eq!(error, NativeError::RevisionConflict);
        assert_eq!(
            metadata::revision_of(
                &traversal::resolve(&root, &["target"], ffi::OpenKind::File).unwrap()
            )
            .unwrap(),
            expected,
            "a DACL-only change must not alter the public revision"
        );
        assert_eq!(
            std::fs::read(sandbox.0.join("target")).unwrap(),
            b"old bytes"
        );
        let mut scan = ffi::DirectoryScan::new();
        let mut names = Vec::new();
        while let Some(batch) = scan.next_batch(&root).unwrap() {
            names.extend(batch);
        }
        assert!(!names
            .iter()
            .any(|name| name.starts_with(INTERNAL_TEMP_PREFIX)));
    }

    #[test]
    fn private_gate_detects_same_name_external_replacement() {
        let sandbox = TestRoot::new("same_name_race");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"old bytes").unwrap();
        let error = replace_bytes_with_hook(&root, &["target"], b"agent bytes", &expected, || {
            std::fs::remove_file(sandbox.0.join("target")).unwrap();
            std::fs::write(sandbox.0.join("target"), b"host bytes").unwrap();
        })
        .unwrap_err();
        assert_eq!(error, NativeError::RevisionConflict);
        assert_eq!(
            std::fs::read(sandbox.0.join("target")).unwrap(),
            b"host bytes"
        );
        let mut scan = ffi::DirectoryScan::new();
        while let Some(batch) = scan.next_batch(&root).unwrap() {
            assert!(!batch
                .iter()
                .any(|name| name.starts_with(INTERNAL_TEMP_PREFIX)));
        }
    }
}
