//! RAII owner for one Windows kernel HANDLE.
//!
//! Every HANDLE returned successfully by the FFI layer is moved into this
//! type immediately; `Drop` closes it on all paths (including panics), so
//! the crate has no manual close site anywhere and cannot leak handles.

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};

pub struct Handle(HANDLE);

impl Handle {
    /// Wrap a just-returned raw handle. `INVALID_HANDLE_VALUE` and NULL are
    /// the two failure encodings Win32 uses; both are rejected here so no
    /// code path can treat them as openable objects.
    pub(crate) fn from_raw(raw: HANDLE) -> Option<Handle> {
        if raw == INVALID_HANDLE_VALUE || raw.is_null() {
            None
        } else {
            Some(Handle(raw))
        }
    }

    pub(crate) fn as_raw(&self) -> HANDLE {
        self.0
    }
}

impl Drop for Handle {
    fn drop(&mut self) {
        // Closing our own owned handle is infallible for our purposes: a
        // false return means the object is already gone; nothing to retry.
        unsafe {
            CloseHandle(self.0);
        }
    }
}

// Kernel objects are process-global: a HANDLE moved across threads still
// refers to the same object, and this crate never shares mutable state
// through the handle itself.
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
