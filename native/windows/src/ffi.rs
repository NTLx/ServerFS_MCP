//! The only `unsafe` surface of the kernel: raw Win32/NT entry points.
//!
//! Every function here either returns an owned [`crate::Handle`] / plain
//! data, or a mapped [`crate::NativeError`]. No raw handle escapes these
//! functions, per the dev_plan §4.8/§13 unsafe policy.

use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows_sys::Wdk::Storage::FileSystem::{
    FileRenameInformation, NtCreateFile, NtSetInformationFile, FILE_CREATE, FILE_DIRECTORY_FILE,
    FILE_ID_BOTH_DIR_INFORMATION, FILE_NON_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT,
    FILE_RENAME_INFORMATION, FILE_SYNCHRONOUS_IO_NONALERT,
};
use windows_sys::Win32::Foundation::{GetLastError, HANDLE, UNICODE_STRING};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FileAttributeTagInfo, FileBasicInfo, FileIdBothDirectoryInfo,
    FileIdBothDirectoryRestartInfo, FileIdInfo, FileStandardInfo, FlushFileBuffers,
    GetFileInformationByHandleEx, ReadFile, SetFileInformationByHandle, SetFilePointerEx,
    WriteFile, FILE_ATTRIBUTE_TAG_INFO, FILE_BASIC_INFO, FILE_BEGIN, FILE_ID_INFO,
    FILE_STANDARD_INFO,
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
pub const CREATE_PARENT_ACCESS: u32 = DIR_TRAVERSE_ACCESS | 0x0000_0006; // ADD_FILE|ADD_SUBDIRECTORY
pub const FILE_READ_ACCESS: u32 = 0x0012_0089; // FILE_GENERIC_READ (includes READ_ATTRIBUTES)
pub const READ_ATTRIBUTES_ONLY: u32 = 0x0000_0080;
const FILE_WRITE_ACCESS: u32 = 0x0013_0196; // FILE_GENERIC_WRITE|DELETE|READ_ATTRIBUTES
const FILE_CREATED_DIRECTORY_ACCESS: u32 = 0x0010_0080; // SYNCHRONIZE|READ_ATTRIBUTES

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
pub fn open_root_by_name(wide_root: &[u16], create_capable: bool) -> Result<Handle, NativeError> {
    let access = if create_capable {
        CREATE_PARENT_ACCESS
    } else {
        DIR_TRAVERSE_ACCESS
    };
    let raw = unsafe {
        CreateFileW(
            wide_root.as_ptr(),
            access,
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

/// Atomically create a single child relative to a retained parent handle.
/// `directory` selects FILE_DIRECTORY_FILE; FILE_CREATE never opens or
/// replaces an existing object. The returned HANDLE is immediately owned.
pub fn create_relative(
    parent: &Handle,
    name: &mut UNICODE_STRING,
    directory: bool,
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
    let mut raw: HANDLE = std::ptr::null_mut();
    let access = if directory {
        FILE_CREATED_DIRECTORY_ACCESS
    } else {
        FILE_WRITE_ACCESS
    };
    let options = if directory {
        FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT
    } else {
        FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
    };
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            access,
            &attrs,
            &mut iosb,
            std::ptr::null(),
            0, // default file attributes
            SHARE_ALL,
            FILE_CREATE,
            options,
            std::ptr::null(),
            0,
        )
    };
    if status != 0 {
        return Err(NativeError::nt(status as u32));
    }
    Handle::from_raw(raw).ok_or(NativeError::Unexpected { code: 0, nt: true })
}

/// Write one chunk to a synchronous file handle. The safe orchestration
/// layer loops until the complete payload has been written.
pub fn write_chunk(handle: &Handle, data: &[u8]) -> Result<usize, NativeError> {
    let mut written = 0u32;
    let ok = unsafe {
        WriteFile(
            handle.as_raw(),
            data.as_ptr(),
            data.len().min(u32::MAX as usize) as u32,
            &mut written,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(NativeError::win32(unsafe { GetLastError() }));
    }
    Ok(written as usize)
}

pub fn flush_file(handle: &Handle) -> Result<(), NativeError> {
    if unsafe { FlushFileBuffers(handle.as_raw()) } == 0 {
        return Err(NativeError::win32(unsafe { GetLastError() }));
    }
    Ok(())
}

/// Publish by renaming the already-open source HANDLE to one simple name
/// rooted at the retained parent HANDLE. NtSetInformationFile avoids the
/// Win32 wrapper's conversion of a relative FileName when RootDirectory is
/// non-null. FileRenameInformation uses ReplaceIfExists = FALSE.
pub fn rename_no_replace(
    source: &Handle,
    parent: &Handle,
    final_name: &[u16],
) -> Result<(), NativeError> {
    let root_offset = align_up(1, std::mem::align_of::<HANDLE>());
    let length_offset = root_offset + std::mem::size_of::<HANDLE>();
    let name_offset = length_offset + std::mem::size_of::<u32>();
    let file_name_bytes = std::mem::size_of_val(final_name);
    let total = (name_offset + file_name_bytes)
        .max(std::mem::size_of::<FILE_RENAME_INFORMATION>() + file_name_bytes.saturating_sub(2));
    let mut storage = vec![0u64; total.div_ceil(std::mem::size_of::<u64>())];
    let info = unsafe {
        std::slice::from_raw_parts_mut(
            storage.as_mut_ptr().cast::<u8>(),
            storage.len() * std::mem::size_of::<u64>(),
        )
    };
    info[0] = 0; // ReplaceIfExists = FALSE
    let root = parent.as_raw() as usize;
    info[root_offset..root_offset + std::mem::size_of::<HANDLE>()]
        .copy_from_slice(&root.to_ne_bytes()[..std::mem::size_of::<HANDLE>()]);
    info[length_offset..name_offset].copy_from_slice(&(file_name_bytes as u32).to_ne_bytes());
    for (index, unit) in final_name.iter().enumerate() {
        let offset = name_offset + index * 2;
        info[offset..offset + 2].copy_from_slice(&unit.to_ne_bytes());
    }
    let mut iosb = IO_STATUS_BLOCK::default();
    let status = unsafe {
        NtSetInformationFile(
            source.as_raw(),
            &mut iosb,
            info.as_ptr().cast(),
            total as u32,
            FileRenameInformation,
        )
    };
    if status < 0 {
        return Err(NativeError::nt(status as u32));
    }
    Ok(())
}

/// Mark an owned temporary object for deletion using its HANDLE.
pub fn mark_for_delete(handle: &Handle) -> Result<(), NativeError> {
    let delete_file = [1u8];
    if unsafe {
        SetFileInformationByHandle(
            handle.as_raw(),
            4, // FileDispositionInfo
            delete_file.as_ptr().cast(),
            delete_file.len() as u32,
        )
    } == 0
    {
        return Err(NativeError::win32(unsafe { GetLastError() }));
    }
    Ok(())
}

fn align_up(value: usize, alignment: usize) -> usize {
    (value + alignment - 1) & !(alignment - 1)
}

/// Read the object's own attribute/reparse tag from the held handle.
pub fn attribute_tag(handle: &Handle) -> Result<FILE_ATTRIBUTE_TAG_INFO, NativeError> {
    let mut info = FILE_ATTRIBUTE_TAG_INFO::default();
    query(handle, FileAttributeTagInfo, &mut info)
}

pub fn basic_info(handle: &Handle) -> Result<FILE_BASIC_INFO, NativeError> {
    let mut info = FILE_BASIC_INFO::default();
    query(handle, FileBasicInfo, &mut info)
}

pub fn file_id(handle: &Handle) -> Result<FILE_ID_INFO, NativeError> {
    let mut info = FILE_ID_INFO::default();
    query(handle, FileIdInfo, &mut info)
}

pub fn standard_info(handle: &Handle) -> Result<FILE_STANDARD_INFO, NativeError> {
    let mut info = FILE_STANDARD_INFO::default();
    query(handle, FileStandardInfo, &mut info)
}

/// One batched handle-relative directory scan via the documented
/// `GetFileInformationByHandleEx` restart/continue class pair
/// (`FileIdBothDirectoryRestartInfo` first, then
/// `FileIdBothDirectoryInfo`). `Ok(true)` means the buffer holds at
/// least one raw entry (last one is terminator-linked); `Ok(false)` is
/// `ERROR_NO_MORE_FILES` — end of list.
pub fn query_directory_batch(
    dir: &Handle,
    buffer: &mut [u8],
    restart_scan: bool,
) -> Result<bool, NativeError> {
    const ERROR_NO_MORE_FILES: u32 = 18;
    let class = if restart_scan {
        FileIdBothDirectoryRestartInfo
    } else {
        FileIdBothDirectoryInfo
    };
    let ok = unsafe {
        GetFileInformationByHandleEx(
            dir.as_raw(),
            class,
            buffer.as_mut_ptr() as *mut core::ffi::c_void,
            buffer.len() as u32,
        )
    };
    if ok != 0 {
        return Ok(true);
    }
    match unsafe { GetLastError() } {
        ERROR_NO_MORE_FILES => Ok(false),
        other => Err(NativeError::win32(other)),
    }
}

/// Raw-directory-scan helper that owns an aligned batch buffer and
/// yields parsed entry names per batch. Keeping the pointer parsing in
/// this module is what lets `enumerate` stay safe code.
pub struct DirectoryScan {
    buffer: AlignedBuffer,
    first: bool,
}

#[repr(align(8))]
struct AlignedBuffer {
    data: [u8; 64 * 1024],
}

impl Default for DirectoryScan {
    fn default() -> Self {
        DirectoryScan::new()
    }
}

impl DirectoryScan {
    pub fn new() -> DirectoryScan {
        DirectoryScan {
            buffer: AlignedBuffer {
                data: [0u8; 64 * 1024],
            },
            first: true,
        }
    }

    /// `Ok(Some(names))` — one batch of entry names (`.`/`..` removed);
    /// `Ok(None)` — the scan is complete.
    pub fn next_batch(&mut self, dir: &Handle) -> Result<Option<Vec<String>>, NativeError> {
        let restart = self.first;
        self.first = false;
        let more = query_directory_batch(dir, &mut self.buffer.data, restart)?;
        if !more {
            return Ok(None);
        }
        let mut names = Vec::new();
        let mut pos: usize = 0;
        let name_offset = std::mem::offset_of!(FILE_ID_BOTH_DIR_INFORMATION, FileName);
        loop {
            let base = pos;
            if base + name_offset > self.buffer.data.len() {
                return Err(NativeError::Unexpected { code: 1, nt: false });
            }
            // The NT contract: batches start at 8-byte-aligned offsets and
            // NextEntryOffset is a multiple of 8; our buffer is 8-aligned.
            let raw = self.buffer.data[base..]
                .as_ptr()
                .cast::<FILE_ID_BOTH_DIR_INFORMATION>();
            let name_bytes = unsafe { (*raw).FileNameLength as usize };
            let end = base + name_offset + name_bytes;
            if end > self.buffer.data.len() {
                return Err(NativeError::Unexpected { code: 2, nt: false });
            }
            let units: Vec<u16> = self.buffer.data[base + name_offset..end]
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .collect();
            let name = String::from_utf16_lossy(&units);
            if name != "." && name != ".." {
                names.push(name);
            }
            let next = unsafe { (*raw).NextEntryOffset };
            if next == 0 {
                break;
            }
            pos += next as usize;
        }
        Ok(Some(names))
    }
}

/// Read the next chunk through a synchronous handle; `Ok(0)` is EOF.
pub fn read_chunk(handle: &Handle, buffer: &mut [u8]) -> Result<usize, NativeError> {
    const ERROR_HANDLE_EOF: u32 = 109;
    let mut read: u32 = 0;
    let ok = unsafe {
        ReadFile(
            handle.as_raw(),
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            &mut read,
            std::ptr::null_mut(), // synchronous handle: system keeps position
        )
    };
    if ok == 0 {
        let last = unsafe { GetLastError() };
        if last == ERROR_HANDLE_EOF {
            return Ok(0);
        }
        return Err(NativeError::win32(last));
    }
    Ok(read as usize)
}

/// Reposition a synchronous handle for the second consistency pass.
pub fn set_position(handle: &Handle, offset: u64) -> Result<(), NativeError> {
    let ok = unsafe {
        SetFilePointerEx(
            handle.as_raw(),
            offset as i64,
            std::ptr::null_mut(),
            FILE_BEGIN,
        )
    };
    if ok == 0 {
        return Err(NativeError::win32(unsafe { GetLastError() }));
    }
    Ok(())
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
