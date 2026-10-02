//! The only `unsafe` surface of the kernel: raw Win32/NT entry points.
//!
//! Every function here either returns an owned [`crate::Handle`] / plain
//! data, or a mapped [`crate::NativeError`]. No raw handle escapes these
//! functions, per the dev_plan §4.8/§13 unsafe policy.

use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows_sys::Wdk::Storage::FileSystem::{
    FileRenameInformation, NtCreateFile, NtQueryEaFile, NtSetInformationFile, FILE_CREATE,
    FILE_DIRECTORY_FILE, FILE_ID_BOTH_DIR_INFORMATION, FILE_NON_DIRECTORY_FILE, FILE_OPEN,
    FILE_OPEN_REPARSE_POINT, FILE_RENAME_INFORMATION, FILE_SYNCHRONOUS_IO_NONALERT,
};
use windows_sys::Win32::Foundation::{GetLastError, HANDLE, UNICODE_STRING};
use windows_sys::Win32::Security::Cryptography::{
    BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
};
use windows_sys::Win32::Security::{
    GetKernelObjectSecurity, GetLengthSid, GetSecurityDescriptorControl,
    GetSecurityDescriptorGroup, GetSecurityDescriptorOwner, SetKernelObjectSecurity,
    DACL_SECURITY_INFORMATION, GROUP_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION,
    PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED,
    UNPROTECTED_DACL_SECURITY_INFORMATION,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FileAttributeTagInfo, FileBasicInfo, FileDispositionInfo, FileIdBothDirectoryInfo,
    FileIdBothDirectoryRestartInfo, FileIdInfo, FileStandardInfo, FileStreamInfo,
    GetFileInformationByHandleEx, ReadFile, SetFileInformationByHandle, SetFilePointerEx,
    WriteFile, FILE_ATTRIBUTE_TAG_INFO, FILE_BASIC_INFO, FILE_BEGIN, FILE_DISPOSITION_INFO,
    FILE_ID_INFO, FILE_STANDARD_INFO,
};
use windows_sys::Win32::System::Ioctl::{FILE_OBJECTID_BUFFER, FSCTL_GET_OBJECT_ID};
use windows_sys::Win32::System::IO::{DeviceIoControl, IO_STATUS_BLOCK};

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

pub const FILE_WRITE_DATA_ACCESS: u32 = 0x0000_0002;
pub const FILE_WRITE_ATTRIBUTES_ACCESS: u32 = 0x0000_0100;
pub const FILE_READ_ATTRIBUTES_ACCESS: u32 = 0x0000_0080;
pub const FILE_DELETE_ACCESS: u32 = 0x0001_0000;
pub const READ_CONTROL_ACCESS: u32 = 0x0002_0000;
pub const WRITE_DAC_ACCESS: u32 = 0x0004_0000;
pub const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;
pub const DIR_MUTATION_ACCESS: u32 = DIR_TRAVERSE_ACCESS | 0x0000_0002 | 0x0000_0004;

const STATUS_NO_EAS_ON_FILE: i32 = windows_sys::Win32::Foundation::STATUS_NO_EAS_ON_FILE;
const ERROR_INSUFFICIENT_BUFFER: u32 = 122;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SecurityDescriptor {
    words: Vec<u32>,
    byte_len: usize,
}

impl SecurityDescriptor {
    fn as_ptr(&self) -> PSECURITY_DESCRIPTOR {
        self.words.as_ptr().cast_mut().cast()
    }

    pub(crate) fn bytes(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.words.as_ptr().cast(), self.byte_len) }
    }

    pub fn owner_group(&self) -> Result<(Vec<u8>, Vec<u8>), NativeError> {
        let mut owner: PSID = std::ptr::null_mut();
        let mut group: PSID = std::ptr::null_mut();
        let mut owner_defaulted = 0;
        let mut group_defaulted = 0;
        if unsafe { GetSecurityDescriptorOwner(self.as_ptr(), &mut owner, &mut owner_defaulted) }
            == 0
            || unsafe {
                GetSecurityDescriptorGroup(self.as_ptr(), &mut group, &mut group_defaulted)
            } == 0
            || owner.is_null()
            || group.is_null()
        {
            return Err(NativeError::MetadataPreservationFailed);
        }
        let owner_len = unsafe { GetLengthSid(owner) } as usize;
        let group_len = unsafe { GetLengthSid(group) } as usize;
        if owner_len == 0 || group_len == 0 {
            return Err(NativeError::MetadataPreservationFailed);
        }
        let owner_bytes = unsafe { std::slice::from_raw_parts(owner.cast::<u8>(), owner_len) };
        let group_bytes = unsafe { std::slice::from_raw_parts(group.cast::<u8>(), group_len) };
        Ok((owner_bytes.to_vec(), group_bytes.to_vec()))
    }

    fn dacl_security_information(&self) -> Result<u32, NativeError> {
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(self.as_ptr(), &mut control, &mut revision) } == 0
        {
            return Err(NativeError::MetadataPreservationFailed);
        }
        Ok(DACL_SECURITY_INFORMATION
            | if control & SE_DACL_PROTECTED != 0 {
                PROTECTED_DACL_SECURITY_INFORMATION
            } else {
                UNPROTECTED_DACL_SECURITY_INFORMATION
            })
    }
}

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
pub fn open_root_by_name(wide_root: &[u16], access: u32) -> Result<Handle, NativeError> {
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

/// Create one new file or directory relative to a verified parent handle.
/// `FILE_CREATE` is atomic and fails if any object already has the name.
pub fn create_relative(
    parent: &Handle,
    name: &mut UNICODE_STRING,
    access: u32,
    directory: bool,
    attributes: u32,
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
    let options = FILE_OPEN_REPARSE_POINT
        | FILE_SYNCHRONOUS_IO_NONALERT
        | if directory {
            FILE_DIRECTORY_FILE
        } else {
            FILE_NON_DIRECTORY_FILE
        };
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            access,
            &attrs,
            &mut iosb,
            std::ptr::null(),
            attributes,
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

pub fn write_chunk(handle: &Handle, bytes: &[u8]) -> Result<usize, NativeError> {
    let length = u32::try_from(bytes.len()).map_err(|_| NativeError::MutationIoError)?;
    let mut written = 0;
    let ok = unsafe {
        WriteFile(
            handle.as_raw(),
            bytes.as_ptr(),
            length,
            &mut written,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(NativeError::MutationIoError);
    }
    Ok(written as usize)
}

pub fn flush_file(handle: &Handle) -> Result<(), NativeError> {
    if unsafe { windows_sys::Win32::Storage::FileSystem::FlushFileBuffers(handle.as_raw()) } == 0 {
        return Err(NativeError::MutationIoError);
    }
    Ok(())
}

pub fn set_basic(handle: &Handle, info: &FILE_BASIC_INFO) -> Result<(), NativeError> {
    if unsafe {
        SetFileInformationByHandle(
            handle.as_raw(),
            FileBasicInfo,
            (info as *const FILE_BASIC_INFO).cast(),
            std::mem::size_of::<FILE_BASIC_INFO>() as u32,
        )
    } == 0
    {
        return Err(NativeError::MetadataPreservationFailed);
    }
    Ok(())
}

pub fn mark_delete(handle: &Handle) -> Result<(), NativeError> {
    let info = FILE_DISPOSITION_INFO { DeleteFile: true };
    if unsafe {
        SetFileInformationByHandle(
            handle.as_raw(),
            FileDispositionInfo,
            (&info as *const FILE_DISPOSITION_INFO).cast(),
            std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
        )
    } == 0
    {
        return Err(NativeError::Unexpected {
            code: unsafe { GetLastError() },
            nt: false,
        });
    }
    Ok(())
}

pub fn rename_relative(
    source: &Handle,
    parent: &Handle,
    name: &str,
    replace: bool,
) -> Result<(), NativeError> {
    let wide: Vec<u16> = name.encode_utf16().collect();
    let prefix = std::mem::offset_of!(FILE_RENAME_INFORMATION, FileName);
    let size = prefix + wide.len() * std::mem::size_of::<u16>();
    let words = size.div_ceil(std::mem::size_of::<u64>());
    let mut storage = vec![0u64; words];
    let info = storage.as_mut_ptr().cast::<FILE_RENAME_INFORMATION>();
    unsafe {
        (*info).Anonymous.ReplaceIfExists = replace as u8 != 0;
        (*info).RootDirectory = parent.as_raw();
        (*info).FileNameLength = (wide.len() * 2) as u32;
        std::ptr::copy_nonoverlapping(
            wide.as_ptr(),
            (info.cast::<u8>().add(prefix)).cast::<u16>(),
            wide.len(),
        );
    }
    let mut iosb = IO_STATUS_BLOCK::default();
    let status = unsafe {
        NtSetInformationFile(
            source.as_raw(),
            &mut iosb,
            info.cast(),
            size as u32,
            FileRenameInformation,
        )
    };
    if status != 0 {
        return Err(NativeError::nt(status as u32));
    }
    Ok(())
}

/// Query the complete stream list from the held file handle. Any query
/// failure is a preservation failure: absence cannot be inferred.
pub fn has_named_streams(handle: &Handle) -> Result<bool, NativeError> {
    #[repr(align(8))]
    struct Buffer([u8; 64 * 1024]);
    let mut buffer = Buffer([0; 64 * 1024]);
    let result = unsafe {
        GetFileInformationByHandleEx(
            handle.as_raw(),
            FileStreamInfo,
            buffer.0.as_mut_ptr().cast(),
            buffer.0.len() as u32,
        )
    };
    if result == 0 {
        // ERROR_HANDLE_EOF means the filesystem returned no stream rows.
        return Err(NativeError::MetadataPreservationFailed);
    }
    let base = std::mem::offset_of!(
        windows_sys::Win32::Storage::FileSystem::FILE_STREAM_INFO,
        StreamName
    );
    let mut position = 0usize;
    let mut saw_default = false;
    let mut named = false;
    loop {
        if position + base > buffer.0.len() {
            return Err(NativeError::MetadataPreservationFailed);
        }
        let entry = unsafe {
            buffer
                .0
                .as_ptr()
                .add(position)
                .cast::<windows_sys::Win32::Storage::FileSystem::FILE_STREAM_INFO>()
        };
        let byte_len = unsafe { (*entry).StreamNameLength as usize };
        if byte_len % 2 != 0 || position + base + byte_len > buffer.0.len() {
            return Err(NativeError::MetadataPreservationFailed);
        }
        let units = unsafe {
            std::slice::from_raw_parts(
                buffer.0.as_ptr().add(position + base).cast::<u16>(),
                byte_len / 2,
            )
        };
        let stream_name = String::from_utf16_lossy(units);
        // NTFS reports the unnamed default stream as `::$DATA`; an empty
        // StreamName is also permitted by the filesystem query contract.
        if stream_name.is_empty() || stream_name.eq_ignore_ascii_case("::$DATA") {
            saw_default = true;
        } else {
            named = true;
        }
        let next = unsafe { (*entry).NextEntryOffset as usize };
        if next == 0 {
            break;
        }
        if next < base || position + next >= buffer.0.len() {
            return Err(NativeError::MetadataPreservationFailed);
        }
        position += next;
    }
    if !saw_default {
        return Err(NativeError::MetadataPreservationFailed);
    }
    Ok(named)
}

pub fn has_ea_data(handle: &Handle) -> Result<bool, NativeError> {
    let mut buffer = [0u8; 64 * 1024];
    let mut iosb = IO_STATUS_BLOCK::default();
    let status = unsafe {
        NtQueryEaFile(
            handle.as_raw(),
            &mut iosb,
            buffer.as_mut_ptr().cast(),
            buffer.len() as u32,
            false,
            std::ptr::null(),
            0,
            std::ptr::null(),
            true,
        )
    };
    if status == STATUS_NO_EAS_ON_FILE {
        return Ok(false);
    }
    if status == 0 || status as u32 == 0x8000_0005 {
        return Ok(iosb.Information > 0 || status as u32 == 0x8000_0005);
    }
    Err(NativeError::MetadataPreservationFailed)
}

pub fn has_object_id(handle: &Handle) -> Result<bool, NativeError> {
    let mut object_id = FILE_OBJECTID_BUFFER::default();
    let mut returned = 0;
    let result = unsafe {
        DeviceIoControl(
            handle.as_raw(),
            FSCTL_GET_OBJECT_ID,
            std::ptr::null(),
            0,
            (&mut object_id as *mut FILE_OBJECTID_BUFFER).cast(),
            std::mem::size_of::<FILE_OBJECTID_BUFFER>() as u32,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    if result != 0 {
        return Ok(true);
    }
    match unsafe { GetLastError() } {
        2 | 1168 => Ok(false), // No object ID is present on this NTFS file.
        _ => Err(NativeError::MetadataPreservationFailed),
    }
}

pub fn security_descriptor(handle: &Handle) -> Result<SecurityDescriptor, NativeError> {
    let requested =
        OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    let mut needed = 0;
    let first = unsafe {
        GetKernelObjectSecurity(
            handle.as_raw(),
            requested,
            std::ptr::null_mut(),
            0,
            &mut needed,
        )
    };
    if first != 0 || unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER || needed == 0 {
        return Err(NativeError::MetadataPreservationFailed);
    }
    let mut descriptor = vec![0u32; (needed as usize).div_ceil(std::mem::size_of::<u32>())];
    if unsafe {
        GetKernelObjectSecurity(
            handle.as_raw(),
            requested,
            descriptor.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(NativeError::MetadataPreservationFailed);
    }
    Ok(SecurityDescriptor {
        words: descriptor,
        byte_len: needed as usize,
    })
}

pub fn apply_dacl(handle: &Handle, descriptor: &SecurityDescriptor) -> Result<(), NativeError> {
    if unsafe {
        SetKernelObjectSecurity(
            handle.as_raw(),
            descriptor.dacl_security_information()?,
            descriptor.as_ptr(),
        )
    } == 0
    {
        return Err(NativeError::MetadataPreservationFailed);
    }
    Ok(())
}

pub fn random_bytes(bytes: &mut [u8]) -> Result<(), NativeError> {
    if unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    } < 0
    {
        return Err(NativeError::Unexpected { code: 1, nt: true });
    }
    Ok(())
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
