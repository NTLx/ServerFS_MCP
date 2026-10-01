//! The PyO3 boundary of the Windows kernel (feature `pyo3`).
//!
//! Contract (dev_plan §11): raw HANDLEs never cross into Python. The only
//! object this module hands out is a `NativeWorkdirSession` that owns the
//! retained root HANDLE internally; every method takes workdir-relative
//! parts and returns plain data, and every kernel failure arrives as a
//! coded `NativeSessionError` — never a raw NTSTATUS or host path.
//!
//! `cargo test` builds without this feature, so the kernel stays testable
//! even where no Python toolchain is present; the wheel build enables it.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::error::NativeError;
use crate::metadata;
use crate::traversal;

pyo3::create_exception!(
    serverfs_windows_native,
    NativeSessionError,
    PyException,
    "ServerFS Windows native kernel failure carrying an agent-safe code"
);

/// Agent-visible (code, message) pairs. Raw NTSTATUS/DWORD values stay
/// inside the Rust [`NativeError::Unexpected`] for debug evidence only:
/// the frozen error boundary forbids native detail in MCP-visible text.
fn error_pair(err: &NativeError) -> (&'static str, String) {
    match err {
        NativeError::PathNotFound => ("PATH_NOT_FOUND", "path not found".to_string()),
        NativeError::NotADirectory => (
            "NOT_A_DIRECTORY",
            "component is not a directory".to_string(),
        ),
        NativeError::IsADirectory => ("IS_A_DIRECTORY", "target is a directory".to_string()),
        NativeError::ReparsePoint => ("REPARSE_POINT", "reparse point refused".to_string()),
        NativeError::AccessDenied => ("ACCESS_DENIED", "access denied".to_string()),
        NativeError::InvalidName => ("INVALID_NAME", "invalid component name".to_string()),
        NativeError::InvalidRoot => ("INVALID_ROOT", "invalid workdir root".to_string()),
        NativeError::Unexpected { .. } => (
            "NATIVE_IO_ERROR",
            "native filesystem operation failed".to_string(),
        ),
    }
}

fn map_error(err: NativeError) -> PyErr {
    let (code, message) = error_pair(&err);
    NativeSessionError::new_err((code, message))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unexpected_native_status_is_redacted_from_agent_visible_text() {
        let err = NativeError::Unexpected {
            code: 0xC000_006D,
            nt: true,
        };
        // internal debug evidence keeps the raw value...
        assert!(err.to_string().contains("0xc000006d"));
        // ...the Python-visible pair never does
        let (code, message) = error_pair(&err);
        assert_eq!(code, "NATIVE_IO_ERROR");
        assert_eq!(message, "native filesystem operation failed");
        assert!(!message.contains("0x"));
    }

    #[test]
    fn stable_codes_for_known_conditions() {
        let cases = [
            (NativeError::PathNotFound, "PATH_NOT_FOUND"),
            (NativeError::NotADirectory, "NOT_A_DIRECTORY"),
            (NativeError::IsADirectory, "IS_A_DIRECTORY"),
            (NativeError::ReparsePoint, "REPARSE_POINT"),
            (NativeError::AccessDenied, "ACCESS_DENIED"),
            (NativeError::InvalidName, "INVALID_NAME"),
            (NativeError::InvalidRoot, "INVALID_ROOT"),
        ];
        for (err, code) in cases {
            assert_eq!(error_pair(&err).0, code);
        }
    }
}

/// A retained workdir-root directory HANDLE with the thread contract
/// proven for exactly that role.
///
/// Root handles are used only as `NtCreateFile` `RootDirectory` anchors
/// and for handle-based metadata queries. They carry no synchronous-IO
/// file-position context (directories are opened without
/// `FILE_SYNCHRONOUS_IO_NONALERT`), and object-manager operations against
/// a shared directory object are thread-safe. Read-optimized file handles
/// are deliberately NOT given this contract: they keep position context
/// and stay confined to one thread until a role-specific proof exists.
struct RootHandle(crate::Handle);
unsafe impl Send for RootHandle {}
unsafe impl Sync for RootHandle {}

/// A bound workdir: owns the retained root HANDLE for the process
/// lifetime. Internal type of the Windows backend; not an agent tool.
#[pyclass(
    frozen,
    name = "NativeWorkdirSession",
    module = "serverfs_windows_native"
)]
pub struct NativeWorkdirSession {
    root: RootHandle,
    read_only: bool,
}

#[pymethods]
impl NativeWorkdirSession {
    /// Whether this workdir was opened read-only (kernel-side enforcement
    /// of the mount-equivalent restriction once mutation channels land).
    #[getter]
    fn read_only(&self) -> bool {
        self.read_only
    }

    /// Volume-qualified object identity of the retained root HANDLE.
    /// Proves session retention: the token is read from the held handle,
    /// never re-resolved from the root name.
    fn object_token(&self) -> Result<String, PyErr> {
        metadata::object_id(&self.root.0)
            .map(|id| id.token())
            .map_err(map_error)
    }
}

/// Open the trusted workdir root and return the retained session object.
///
/// `root` must be a local drive path (`X:\...`); everything else — UNC,
/// device and global-object namespaces, trailing separators on
/// directories — is refused as `INVALID_ROOT` without any normalization.
#[pyfunction]
fn open_workdir(root: &str, read_only: bool) -> Result<NativeWorkdirSession, PyErr> {
    let handle = traversal::open_root(root).map_err(map_error)?;
    Ok(NativeWorkdirSession {
        root: RootHandle(handle),
        read_only,
    })
}

#[pymodule]
fn serverfs_windows_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_workdir, m)?)?;
    m.add(
        "NativeSessionError",
        m.py().get_type::<NativeSessionError>(),
    )?;
    Ok(())
}
