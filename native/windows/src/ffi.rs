//! The only `unsafe` surface of the kernel: raw Win32/NT entry points.
//!
//! Every function here either returns an owned [`crate::Handle`] / plain
//! data, or a mapped [`crate::NativeError`]. No raw handle escapes these
//! functions, per the dev_plan §4.8/§13 unsafe policy.

use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows_sys::Wdk::Storage::FileSystem::{
    NtCreateFile, FILE_DIRECTORY_FILE, FILE_NON_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT,
    FILE_SYNCHRONOUS_IO_NONALERT,
};
use windows_sys::Win32::Foundation::{GetLastError, HANDLE, UNICODE_STRING};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandleEx, FILE_ATTRIBUTE_TAG_INFO, FILE_BASIC_INFO,
    FILE_ID_INFO,
};
use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

use crate::error::NativeError;
use crate::handle::Handle;

// Win32 constants not re-exported by the selected windows-sys modules.
const OPEN_EXISTING: u32 = 3;
const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
// Required by Win32 to open a directory as a directory handle at all. It
// bypasses ACLs only when the process holds SeBackupPrivilege, which the
// ServerFS process never requests or holds: for us it changes nothing
// except making directory opens possible. NT-level relative opens
// (NtCreateFile) do not need this case at all.
const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
const OBJ_CASE_INSENSITIVE: u32 = 0x0000_0040;

/// Access masks used by the traversal kernel.
pub const DIR_TRAVERSE_ACCESS: u32 = 0x0000_00A1; // FILE_LIST_DIRECTORY|FILE_TRAVERSE|FILE_READ_ATTRIBUTES
pub const FILE_READ_ACCESS: u32 = 0x0012_0089; // FILE_GENERIC_READ (includes READ_ATTRIBUTES)
pub const READ_ATTRIBUTES_ONLY: u32 = 0x0000_0080;

const SHARE_ALL: u32 = 0x0000_0007; // READ|WRITE|DELETE

/// Create-options combinations for the three component expectations.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum OpenKind {
    /// Component must be a real directory (intermediate or listed dir).
    Directory,
    /// Component must be a real file (leaf reads/updates).
    File,
    /// Type is decided from the returned handle's own attributes.
    Any,
}

pub fn create_options_for(kind: OpenKind) -> u32 {
    // FILE_OPEN_REPARSE_POINT on every open: a reparse object is opened AS
    // itself so the caller can refuse it; it is never followed by the
    // kernel on our behalf.
    let base = FILE_OPEN_REPARSE_POINT;
    match kind {
        OpenKind::Directory => base | FILE_DIRECTORY_FILE,
        OpenKind::File => base | FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
        OpenKind::Any => base,
    }
}

/// Open the workdir root — the only path string the kernel ever resolves.
///
/// `wide_root` must come from [`crate::path::encoded_root`] (`\\?\` literal
/// namespace). The handle is opened for reparse inspection and validated
/// by the caller.
pub fn open_root_by_name(wide_root: &[u16]) -> Result<Handle, NativeError> {
    let raw = unsafe {
        CreateFileW(
            wide_root.as_ptr(),
            DIR_TRAVERSE_ACCESS,
            SHARE_ALL,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            std::ptr::null_mut(),
        )
    };
    Handle::from_raw(raw).ok_or_else(|| NativeError::win32(unsafe { GetLastError() }))
}

/// Open one already-validated component relative to a trusted parent
/// directory handle. The prototype surface opens existing objects only;
/// creation dispositions arrive with the mutation kernel.
pub fn open_relative(
    parent: &Handle,
    name: &mut UNICODE_STRING,
    access: u32,
    kind: OpenKind,
) -> Result<Handle, NativeError> {
    let mut iosb = IO_STATUS_BLOCK::default();
    let attrs = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.as_raw(),
        ObjectName: name as *mut UNICODE_STRING,
        Attributes: OBJ_CASE_INSENSITIVE,
        SecurityDescriptor: std::ptr::null(),
        SecurityQualityOfService: std::ptr::null(),
    };

    let mut handle: HANDLE = std::ptr::null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            access,
            &attrs,
            &mut iosb,
            std::ptr::null(), // AllocationSize: ignored for open
            0,                // FileAttributes
            SHARE_ALL,
            FILE_OPEN,
            create_options_for(kind),
            std::ptr::null(), // EaBuffer
            0,                // EaLength
        )
    };
    if status != 0 {
        return Err(NativeError::nt(status as u32));
    }
    Handle::from_raw(handle).ok_or(NativeError::Unexpected { code: 0, nt: true })
}

/// Read the object's own attribute/reparse tag from the held handle.
pub fn attribute_tag(handle: &Handle) -> Result<FILE_ATTRIBUTE_TAG_INFO, NativeError> {
    let mut info = FILE_ATTRIBUTE_TAG_INFO::default();
    query(handle, 9 /* FileAttributeTagInfo */, &mut info)
}

pub fn basic_info(handle: &Handle) -> Result<FILE_BASIC_INFO, NativeError> {
    let mut info = FILE_BASIC_INFO::default();
    query(handle, 0 /* FileBasicInfo */, &mut info)
}

pub fn file_id(handle: &Handle) -> Result<FILE_ID_INFO, NativeError> {
    let mut info = FILE_ID_INFO::default();
    query(handle, 18 /* FileIdInfo */, &mut info)
}

fn query<T: Default>(handle: &Handle, class: i32, out: &mut T) -> Result<T, NativeError> {
    let ok = unsafe {
        GetFileInformationByHandleEx(
            handle.as_raw(),
            class,
            out as *mut T as *mut core::ffi::c_void,
            std::mem::size_of::<T>() as u32,
        )
    };
    if ok == 0 {
        return Err(NativeError::win32(unsafe { GetLastError() }));
    }
    Ok(std::mem::take(out))
}
