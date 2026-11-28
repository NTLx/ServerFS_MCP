//! Identity, metadata and opaque revision read from a held handle — never
//! from a path.
//!
//! `FILE_ID_INFO` is the Windows counterpart of the `(st_dev, st_ino)`
//! pair the Linux revision token hashes: it identifies the object, not the
//! name. The revision digest is computed here, inside the kernel: raw
//! volume serials and file IDs never cross the Python boundary (§11
//! revision ownership).
//!
//! Material (dev_plan §15: "frozen only after a Windows probe"): the probe
//! on real NTFS froze the tuple as volume serial + 128-bit file id +
//! attributes + size + link count + last-write time. `ChangeTime` was
//! measured in and then OUT: NTFS moves it on some external renames, and
//! rename-stability is a frozen revision property. Measured guarantees
//! live in `tests/ntfs_read_kernel.rs`: content edits move it,
//! metadata-only attribute changes move it, renames keep it, and a
//! same-name replacement always moves it with the object identity itself.

use sha2::{Digest, Sha256};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
};

use crate::error::NativeError;
use crate::{ffi, Handle};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ObjectId {
    pub volume_serial: u64,
    pub file_id: u128,
}

impl ObjectId {
    /// Stable textual identity for cross-checks and future revision
    /// material. Volume-qualified so two roots on different volumes cannot
    /// collide.
    pub fn token(&self) -> String {
        format!("vol-{:016x}:file-{:032x}", self.volume_serial, self.file_id)
    }
}

pub fn object_id(handle: &Handle) -> Result<ObjectId, NativeError> {
    let info = ffi::file_id(handle)?;
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&info.FileId.Identifier);
    Ok(ObjectId {
        volume_serial: info.VolumeSerialNumber,
        file_id: u128::from_le_bytes(bytes),
    })
}

/// Everything the read channels need about one object, from its handle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NativeMetadata {
    pub id: ObjectId,
    pub is_directory: bool,
    pub is_reparse: bool,
    /// End-of-file in bytes; `None` for directories (matches the Linux
    /// contract that only regular files carry a size).
    pub size: Option<u64>,
    pub link_count: u32,
    /// FILETIME-style 100ns intervals since 1601-01-01.
    pub last_write_100ns: i64,
    pub attributes: u32,
}

impl NativeMetadata {
    /// The additive v0.10 Windows entry type (§14): reparse objects are
    /// named honestly instead of as Unix symlinks.
    pub fn type_label(&self) -> &'static str {
        if self.is_reparse {
            "reparse_point"
        } else if self.is_directory {
            "directory"
        } else {
            "file"
        }
    }

    /// Opaque revision token, backend-owned: `v1:<16 hex>` of a SHA-256
    /// over the frozen material tuple. Mirrors the Linux token shape so
    /// clients and tests keep matching `v1:[0-9a-f]{16}`.
    pub fn revision(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.id.volume_serial.to_le_bytes());
        hasher.update(self.id.file_id.to_le_bytes());
        hasher.update(self.attributes.to_le_bytes());
        hasher.update(self.size.unwrap_or(0).to_le_bytes());
        hasher.update(self.link_count.to_le_bytes());
        hasher.update(self.last_write_100ns.to_le_bytes());
        let digest = hasher.finalize();
        let hex: String = digest[..8].iter().map(|b| format!("{b:02x}")).collect();
        format!("v1:{hex}")
    }
}

/// Collect identity and metadata from the held handle via three
/// handle-information classes. One function so every channel reads the
/// same object snapshot.
pub fn collect(handle: &Handle) -> Result<NativeMetadata, NativeError> {
    let tag = ffi::attribute_tag(handle)?;
    let basic = ffi::basic_info(handle)?;
    let standard = ffi::standard_info(handle)?;
    let id = object_id(handle)?;
    let is_directory = tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    Ok(NativeMetadata {
        id,
        is_directory,
        is_reparse: tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0,
        size: if is_directory {
            None
        } else {
            Some(standard.EndOfFile as u64)
        },
        link_count: standard.NumberOfLinks,
        last_write_100ns: basic.LastWriteTime,
        attributes: basic.FileAttributes,
    })
}

pub fn revision_of(handle: &Handle) -> Result<String, NativeError> {
    Ok(collect(handle)?.revision())
}

pub fn is_directory(handle: &Handle) -> Result<bool, NativeError> {
    Ok(ffi::attribute_tag(handle)?.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0)
}
