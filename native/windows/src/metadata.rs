//! Identity and metadata read from a held handle — never from a path.
//!
//! `FILE_ID_INFO` is the Windows counterpart of the `(st_dev, st_ino)`
//! pair the Linux revision token hashes: it identifies the object, not the
//! name, and stays stable across renames of the name.

use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_DIRECTORY;

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

pub fn is_directory(handle: &Handle) -> Result<bool, NativeError> {
    Ok(ffi::attribute_tag(handle)?.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0)
}
