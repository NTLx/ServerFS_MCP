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
use crate::{ffi, metadata, read, traversal, Handle};

const TEMP_ATTEMPTS: usize = 16;
const WRITE_CHUNK: usize = 64 * 1024;
pub const INTERNAL_TEMP_PREFIX: &str = ".serverfs-tmp-";
/// Share mask for handles held across a mutation window (§19.4): others
/// keep read access — every channel still filters identically and
/// concurrent ServerFS readers are never blocked — while new WRITE opens
/// and new DELETE opens (delete or ordinary path rename both require
/// DELETE access) are refused while the handle lives. Used by the
/// deletion paths, the created directory and every temp object.
///
/// This is defense in depth that narrows the final-check-to-commit
/// interval. It is not proof against a POSIX-style rename primitive or
/// an arbitrary hostile same-user process; the name-relative final
/// identity/revision gate is what detects drift those paths can still
/// cause, and neither backend claims compare-and-swap semantics.
const MUTATION_HELD_SHARE: u32 = ffi::FILE_SHARE_READ;
/// Replacement targets additionally share DELETE. Measured on real NTFS
/// (WorkPC): the atomic `FileRenameInformationEx` publication cannot
/// replace a destination whose open handles do not grant FILE_SHARE_DELETE
/// — holding the strict read-only share mask to the commit would deadlock
/// our own rename (STATUS_SHARING_VIOLATION). New external WRITERS are
/// still refused across the whole window; an external delete or rename
/// that exploits the retained DELETE share is detected by the
/// name-relative final gate before publication.
const REPLACEMENT_HELD_SHARE: u32 = ffi::FILE_SHARE_READ | ffi::FILE_SHARE_DELETE;
const SAFE_BASIC_ATTRIBUTES: u32 = FILE_ATTRIBUTE_READONLY
    | FILE_ATTRIBUTE_HIDDEN
    | FILE_ATTRIBUTE_SYSTEM
    | FILE_ATTRIBUTE_ARCHIVE
    | FILE_ATTRIBUTE_NORMAL
    | FILE_ATTRIBUTE_TEMPORARY
    | FILE_ATTRIBUTE_NOT_CONTENT_INDEXED;
// The original is opened only for revision and preservation queries. The
// temporary source handle carries DELETE for the relative replacement rename.
const REPLACEMENT_ACCESS: u32 = ffi::FILE_READ_ACCESS | ffi::FILE_READ_ATTRIBUTES_ACCESS;
// Linux `delete_file` opens the target read-only (fdio open_regular_at) and
// refuses a file the process cannot read before unlinking it — directory
// write permission alone must never delete an unreadable file. FILE_READ_DATA
// rides on the Windows delete open to enforce the same product property;
// ACCESS_DENIED at open is the refusal, and nothing else is attempted.
const DELETE_ACCESS: u32 = ffi::FILE_READ_ACCESS | ffi::FILE_DELETE_ACCESS;
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
    size: u64,
    link_count: u32,
    security_descriptor: ffi::SecurityDescriptor,
    owner_sid: Vec<u8>,
    group_sid: Vec<u8>,
    unsupported: UnsupportedState,
}

impl PreservationSnapshot {
    /// Private drift material for the replacement final gate. Unlike the
    /// public revision tuple this never leaves the kernel, so it may carry
    /// `ChangeTime` (we never rename the target ourselves) and carries
    /// size/link count explicitly. `LastAccessTime` is deliberately
    /// excluded: measured on WorkPC NTFS, pure concurrent READS move it,
    /// and a read is not a mutation — including it made honest
    /// read/write overlap fail with a spurious `RevisionConflict`.
    fn fingerprint(&self) -> [u8; 32] {
        let mut digest = Sha256::new();
        digest.update(self.id.volume_serial.to_le_bytes());
        digest.update(self.id.file_id.to_le_bytes());
        digest.update(self.basic.CreationTime.to_le_bytes());
        digest.update(self.basic.LastWriteTime.to_le_bytes());
        digest.update(self.basic.ChangeTime.to_le_bytes());
        digest.update(self.basic.FileAttributes.to_le_bytes());
        digest.update(self.size.to_le_bytes());
        digest.update(self.link_count.to_le_bytes());
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
    share_mode: u32,
) -> Result<Handle, NativeError> {
    traversal::open_component_with_share(parent, name, kind, access, share_mode)
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
        size: md.size.unwrap_or(0),
        link_count: md.link_count,
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
            MUTATION_HELD_SHARE,
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
    // Verify on the handle we are about to publish: the applied security
    // must carry the same preservable material as the original. Raw byte
    // equality would demand inheriting NTFS' AUTO_INHERITED marker, which
    // an explicit DACL application legitimately re-records; `equivalent`
    // masks only those recomputable control flags.
    if !ffi::security_descriptor(temp)?.equivalent(&snapshot.security_descriptor) {
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

/// The file-target gate, in the order both backends must answer.
///
/// Object kind is decided before the revision guard: a reparse object is refused because policy
/// forbids entering it, a directory is refused because the channel takes a file, and either answer
/// must win over `RevisionConflict` — a directory's or junction's revision never equals a stale
/// file token, so checking the guard first turned a kind error into `RevisionConflict`, which is
/// the divergence dev_plan_v0.11 §15 C0.7 closes. Reparse precedes directory for the same reason
/// `NativeMetadata::entry_type` does: a junction is a reparse object that also happens to look like
/// a directory, and the precise policy code is the useful one.
fn check_file_target(
    handle: &Handle,
    expected: &str,
) -> Result<metadata::NativeMetadata, NativeError> {
    let md = metadata::collect(handle)?;
    if md.is_reparse {
        return Err(NativeError::ReparsePoint);
    }
    if md.is_directory {
        return Err(NativeError::IsADirectory);
    }
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
    // NTFS may finalize LastWriteTime only when the final writable handle
    // closes. The public revision includes LastWriteTime, so deriving it
    // from the still-open staging handle can return a token that is stale
    // immediately after this function returns. Close first, then reopen the
    // published name through the retained parent and report that stable state.
    drop(temp);
    let published = traversal::open_component(parent, name, ffi::OpenKind::File)?;
    metadata::revision_of(&published)
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
    let original = open_leaf(
        parent,
        name,
        REPLACEMENT_ACCESS,
        ffi::OpenKind::File,
        REPLACEMENT_HELD_SHARE,
    )?;
    let initial_md = check_file_target(&original, expected_revision)?;
    publish_replacement(
        parent,
        name,
        &original,
        &initial_md,
        expected_revision,
        bytes,
        before_gate,
    )
}

/// Stage, preserve, re-verify and atomically publish a replacement for a target the caller is
/// already holding and has already validated.
///
/// Every caller reaches this with `original` still open, which is what makes the restricted-share
/// hold span the whole window from validation to commit — including the source read the edit
/// channel performs (`replace_source_with`), so no external writer can be live against the bytes
/// the edit was computed from.
fn publish_replacement(
    parent: &Handle,
    name: &str,
    original: &Handle,
    initial_md: &metadata::NativeMetadata,
    expected_revision: &str,
    bytes: &[u8],
    before_gate: impl FnOnce(),
) -> Result<String, NativeError> {
    let snapshot = capture_snapshot(original)?;
    if snapshot.id != initial_md.id || snapshot.has_unsupported_state() {
        return Err(NativeError::MetadataPreservationFailed);
    }
    let initial_fingerprint = snapshot.fingerprint();
    let (mut temp, _) = create_temp(parent)?;
    write_and_flush(&temp.handle, bytes)?;
    apply_snapshot(&temp.handle, &snapshot)?;
    ffi::flush_file(&temp.handle).map_err(|_| NativeError::MutationIoError)?;

    before_gate();
    let final_target = match traversal::open_component_with_access(
        parent,
        name,
        ffi::OpenKind::File,
        REPLACEMENT_ACCESS,
    ) {
        Ok(handle) => handle,
        Err(NativeError::PathNotFound) => return Err(NativeError::PathNotFound),
        Err(_) => return Err(NativeError::RevisionConflict),
    };
    let final_md = metadata::collect(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.is_directory {
        return Err(NativeError::IsADirectory);
    }
    let final_snapshot =
        capture_snapshot(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.id != initial_md.id
        || final_md.revision() != expected_revision
        || final_snapshot.fingerprint() != initial_fingerprint
        || final_snapshot.unsupported != snapshot.unsupported
    {
        return Err(NativeError::RevisionConflict);
    }
    // POSIX replacement semantics let NTFS atomically replace an open
    // name; existing handles continue to reference the old object. The
    // destination's open handles must grant FILE_SHARE_DELETE for the
    // replacement to land (measured on WorkPC NTFS), which is why
    // replacement targets hold READ|DELETE instead of the strict delete
    // mask. The documented non-cooperating-writer race remains between
    // this gate and the rename.
    ffi::rename_relative(&temp.handle, parent, name, true)?;
    temp.published = true;
    // As with create, close the writable staging handle before computing the
    // externally visible revision. This also avoids querying metadata from a
    // renamed staging handle after the replacement has already committed.
    drop(temp);
    let published = traversal::open_component(parent, name, ffi::OpenKind::File)?;
    metadata::revision_of(&published)
}

/// Why a source transaction stopped. Either the kernel's own condition, or whatever the caller's
/// transform raised — the caller's error is never rewritten into a filesystem error.
#[derive(Debug)]
pub enum SourceError<E> {
    Native(NativeError),
    Callback(E),
}

impl<E> From<NativeError> for SourceError<E> {
    fn from(value: NativeError) -> Self {
        SourceError::Native(value)
    }
}

/// The edit channel's single transaction: validate the target, read its bytes from the handle the
/// replacement holds, build the replacement from them, and publish — without ever releasing the
/// restricted-share hold between the read and the commit.
///
/// This is C0 completion contract item 5 in `dev_plan_v0.11.md` §15. It narrows the
/// **active-writer** race: while this transaction is open, an external process that still wants
/// `WRITE` access cannot open the target at all, so the bytes the edit was computed from cannot be
/// under a live writer. It does not close the same-tick same-size token alias, which contract
/// decision B accepts as a documented boundary, and no claim here says otherwise.
pub fn replace_source_with<E>(
    root: &Handle,
    parts: &[&str],
    expected_revision: &str,
    max_read_bytes: u64,
    transform: impl FnOnce(Vec<u8>, String) -> Result<Vec<u8>, E>,
) -> Result<String, SourceError<E>> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let original = open_leaf(
        parent,
        name,
        REPLACEMENT_ACCESS,
        ffi::OpenKind::File,
        REPLACEMENT_HELD_SHARE,
    )?;
    let initial_md = check_file_target(&original, expected_revision)?;
    let source = read::read_open(&original, max_read_bytes)?;
    if source.metadata.id != initial_md.id || source.metadata.revision() != expected_revision {
        return Err(NativeError::RevisionConflict.into());
    }
    let revision_before = source.metadata.revision();
    let payload = transform(source.data, revision_before).map_err(SourceError::Callback)?;
    publish_replacement(
        parent,
        name,
        &original,
        &initial_md,
        expected_revision,
        &payload,
        || {},
    )
    .map_err(SourceError::Native)
}

pub fn delete_file(
    root: &Handle,
    parts: &[&str],
    expected_revision: &str,
) -> Result<(u64, String), NativeError> {
    let name = target_name(parts)?;
    let owned_parent = resolve_parent(root, parts)?;
    let parent = parent_handle(root, &owned_parent);
    let original = open_leaf(
        parent,
        name,
        DELETE_ACCESS,
        ffi::OpenKind::File,
        MUTATION_HELD_SHARE,
    )?;
    let initial = check_file_target(&original, expected_revision)?;
    // NT sharing is bidirectional (measured on WorkPC NTFS): a new open
    // must ALSO grant sharing that covers every already-held handle's
    // access, so the gate helper opens with the full SHARE_ALL mask even
    // though only the holder's restrictive mask hardens the window. The
    // holder still refuses new external WRITE (and, on the delete paths,
    // DELETE) opens: every existing handle must consent, and the holder
    // does not.
    let final_target = traversal::open_component_with_access(
        parent,
        name,
        ffi::OpenKind::File,
        ffi::FILE_READ_ACCESS,
    )
    .map_err(|error| {
        if error == NativeError::PathNotFound {
            error
        } else {
            NativeError::RevisionConflict
        }
    })?;
    let final_md = metadata::collect(&final_target).map_err(|_| NativeError::RevisionConflict)?;
    if final_md.is_directory {
        return Err(NativeError::IsADirectory);
    }
    if final_md.id != initial.id || final_md.revision() != expected_revision {
        return Err(NativeError::RevisionConflict);
    }
    // Deletion applies to the verified object handle. If a non-cooperating
    // writer renames it after the final gate, this removes that object at
    // its new name rather than deleting an unverified replacement. The
    // gate handle must be released before returning: disposition executes
    // at LAST close, so keeping any open handle on the object alive would
    // leave the delete pending forever (measured on WorkPC NTFS).
    let result = (final_md.size.unwrap_or(0), final_md.revision());
    ffi::mark_delete(&original)?;
    drop(final_target);
    Ok(result)
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
        MUTATION_HELD_SHARE,
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
        MUTATION_HELD_SHARE,
    )?;
    let initial = check_expected(&original, expected_revision)?;
    if !is_empty(&original)? {
        return Err(NativeError::DirectoryNotEmpty);
    }
    let final_target = traversal::open_component_with_access(
        parent,
        name,
        ffi::OpenKind::Directory,
        ffi::DIR_TRAVERSE_ACCESS,
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

    /// The build step's return type, spelled out so a test closure that only panics or only fails
    /// still pins the transaction's error type.
    type Build = Result<Vec<u8>, &'static str>;

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
    fn directory_target_is_refused_before_the_revision_guard() {
        // dev_plan_v0.11 §15 C0.7. A directory answer must be IsADirectory whether the caller's
        // token is stale or current: the stale case used to answer RevisionConflict because the
        // guard ran before the type decision, which is the Linux/native divergence.
        let sandbox = TestRoot::new("directory_precedence");
        let root = sandbox.open();
        std::fs::create_dir(sandbox.0.join("sub")).unwrap();
        let stale = "v1:0000000000000000";
        assert_eq!(
            replace_bytes(&root, &["sub"], b"bytes", stale).unwrap_err(),
            NativeError::IsADirectory
        );
        assert_eq!(
            delete_file(&root, &["sub"], stale).unwrap_err(),
            NativeError::IsADirectory
        );
        let current = metadata::revision_of(
            &traversal::resolve(&root, &["sub"], ffi::OpenKind::Directory).unwrap(),
        )
        .unwrap();
        assert_eq!(
            replace_bytes(&root, &["sub"], b"bytes", &current).unwrap_err(),
            NativeError::IsADirectory
        );
        assert_eq!(
            delete_file(&root, &["sub"], &current).unwrap_err(),
            NativeError::IsADirectory
        );
        assert!(sandbox.0.join("sub").is_dir());
        let mut scan = ffi::DirectoryScan::new();
        while let Some(batch) = scan.next_batch(&root).unwrap() {
            assert!(!batch
                .iter()
                .any(|name| name.starts_with(INTERNAL_TEMP_PREFIX)));
        }
    }

    #[test]
    fn source_transaction_reads_the_held_target_and_publishes_the_build() {
        // dev_plan_v0.11 §15, C0 completion contract item 5: the edit channel's read happens on the
        // object the replacement is holding, so an external writer that still wants WRITE access is
        // refused for the whole window from validation to commit.
        let sandbox = TestRoot::new("source_transaction");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"old bytes").unwrap();
        let seen: std::cell::RefCell<Option<Vec<u8>>> = std::cell::RefCell::new(None);
        let revision = replace_source_with(
            &root,
            &["target"],
            &expected,
            4096,
            |data, before| -> Build {
                assert_eq!(before, expected);
                let probe = std::fs::OpenOptions::new()
                    .write(true)
                    .open(sandbox.0.join("target"));
                assert!(probe.is_err(), "the hold must refuse a new writer");
                *seen.borrow_mut() = Some(data.clone());
                Ok(b"edited bytes".to_vec())
            },
        )
        .unwrap();
        assert_eq!(*seen.borrow(), Some(b"old bytes".to_vec()));
        assert_eq!(
            std::fs::read(sandbox.0.join("target")).unwrap(),
            b"edited bytes"
        );
        assert_eq!(
            metadata::revision_of(
                &traversal::resolve(&root, &["target"], ffi::OpenKind::File).unwrap()
            )
            .unwrap(),
            revision
        );
        // A new writer is admitted again once the transaction is over.
        std::fs::OpenOptions::new()
            .write(true)
            .open(sandbox.0.join("target"))
            .unwrap();
    }

    #[test]
    fn source_transaction_validates_before_reading_anything() {
        let sandbox = TestRoot::new("source_transaction_preconditions");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"payload").unwrap();
        let stale = replace_source_with(
            &root,
            &["target"],
            "v1:0000000000000000",
            4096,
            |_, _| -> Build {
                panic!("a stale token must not reach the read");
            },
        )
        .unwrap_err();
        assert!(matches!(
            stale,
            SourceError::Native(NativeError::RevisionConflict)
        ));

        std::fs::create_dir(sandbox.0.join("sub")).unwrap();
        for token in ["v1:0000000000000000", &expected] {
            let error = replace_source_with(&root, &["sub"], token, 4096, |_, _| -> Build {
                panic!("a directory target must not reach the read");
            })
            .unwrap_err();
            assert!(
                matches!(error, SourceError::Native(NativeError::IsADirectory)),
                "type must be answered before the revision guard, with token {token}"
            );
        }

        let too_large = replace_source_with(&root, &["target"], &expected, 4, |_, _| -> Build {
            panic!("an over-bound source must not be transformed")
        })
        .unwrap_err();
        assert!(matches!(
            too_large,
            SourceError::Native(NativeError::FileTooLarge)
        ));
        assert_eq!(std::fs::read(sandbox.0.join("target")).unwrap(), b"payload");
    }

    #[test]
    fn source_transaction_aborts_without_publishing_when_the_build_fails() {
        // A text-edit refusal is the caller's condition, not a filesystem failure: nothing may be
        // written and no staging entry may survive.
        let sandbox = TestRoot::new("source_transaction_abort");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"payload").unwrap();
        let error = replace_source_with(&root, &["target"], &expected, 4096, |_, _| -> Build {
            Err("TEXT_EDIT_NOT_FOUND")
        })
        .unwrap_err();
        assert!(matches!(
            error,
            SourceError::Callback("TEXT_EDIT_NOT_FOUND")
        ));
        assert_eq!(std::fs::read(sandbox.0.join("target")).unwrap(), b"payload");
        let mut scan = ffi::DirectoryScan::new();
        while let Some(batch) = scan.next_batch(&root).unwrap() {
            assert!(!batch
                .iter()
                .any(|name| name.starts_with(INTERNAL_TEMP_PREFIX)));
        }
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
        // Simulates the non-cooperating-writer class that sharing mode
        // cannot block: an external process performing its own
        // POSIX-style HANDLE-relative replacement of the name between
        // our snapshot and the final gate. The final identity/revision/
        // fingerprint gate must refuse publication (no temp debris, the
        // external bytes stay), proving the gate — not that the race
        // interval is closed.
        let sandbox = TestRoot::new("same_name_race");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["target"], b"old bytes").unwrap();
        let error = replace_bytes_with_hook(&root, &["target"], b"agent bytes", &expected, || {
            std::fs::write(sandbox.0.join("stager"), b"host bytes").unwrap();
            let stager = traversal::open_component_with_access(
                &root,
                "stager",
                ffi::OpenKind::File,
                TEMP_ACCESS,
            )
            .unwrap();
            ffi::rename_relative(&stager, &root, "target", true).unwrap();
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

    #[test]
    fn replacement_window_blocks_external_write_opens() {
        // Sharing-mode defense in depth inside the real replacement
        // window: while the replacement holds its target handles
        // (READ|DELETE share), an ordinary external WRITE open is
        // refused and read-style opens still succeed. The mutation then
        // publishes normally. This is Win32-writer hardening evidence
        // only — not a compare-and-swap claim against hostile
        // same-user processes (see the POSIX-rename test above).
        let sandbox = TestRoot::new("window_share");
        let root = sandbox.open();
        let expected = create_bytes(&root, &["win.txt"], b"old").unwrap();
        let target = sandbox.0.join("win.txt");
        let replacement = replace_bytes_with_hook(&root, &["win.txt"], b"new", &expected, || {
            let denied = std::fs::OpenOptions::new()
                .write(true)
                .open(&target)
                .expect_err("external WRITE open must be refused mid-window");
            assert_eq!(denied.raw_os_error(), Some(32)); // ERROR_SHARING_VIOLATION
            assert_eq!(std::fs::read(&target).unwrap(), b"old");
        });
        replacement.unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"new");
        std::fs::OpenOptions::new()
            .write(true)
            .open(&target)
            .expect("WRITE open succeeds after the mutation released its handles");
    }
}
