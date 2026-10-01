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
        // Linux file contexts render "target is a directory" as NOT_A_FILE
        // (open-by-name on a directory yields EISDIR); keep the code identical
        NativeError::IsADirectory => ("NOT_A_FILE", "target is a directory".to_string()),
        NativeError::ReparsePoint => ("REPARSE_POINT", "reparse point refused".to_string()),
        NativeError::AccessDenied => ("ACCESS_DENIED", "access denied".to_string()),
        NativeError::InvalidName => ("INVALID_NAME", "invalid component name".to_string()),
        NativeError::InvalidRoot => ("INVALID_ROOT", "invalid workdir root".to_string()),
        NativeError::FileTooLarge => ("FILE_TOO_LARGE", err.to_string()),
        NativeError::ChangedDuringRead => ("FILE_CHANGED_DURING_READ", err.to_string()),
        NativeError::LineTooLarge { .. } => ("LINE_TOO_LARGE", err.to_string()),
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
            (NativeError::IsADirectory, "NOT_A_FILE"),
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

/// One reported directory row: (name, type, size|None, last-write 100ns).
type ListedRow = (String, String, Option<u64>, i64);

/// The backend-neutral TextPage contract as a plain tuple.
type PageTuple = (String, Vec<Vec<u8>>, u64, u64, bool, bool, bool);

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

    /// Report the target object: (type, size|None, last-write 100ns,
    /// revision). Intermediates must be real non-reparse directories;
    /// the final component is classified from its own handle (§14).
    fn stat(&self, parts: Vec<String>) -> Result<(String, Option<u64>, i64, String), PyErr> {
        let md = self.with_target(&parts, metadata::collect)?;
        Ok((
            md.type_label().to_string(),
            md.size,
            md.last_write_100ns,
            md.revision(),
        ))
    }

    /// Candidate names from one batched scan of the directory at `parts`
    /// (empty = the retained root), each verified by a HANDLE-relative
    /// re-open; raced-away entries are skipped like Linux `scandir`.
    /// Returns (name, type, size|None, last-write 100ns) tuples in scan
    /// order — sorting and policy filtering are the session layer's job.
    fn list(&self, parts: Vec<String>) -> Result<Vec<ListedRow>, PyErr> {
        let listed = if parts.is_empty() {
            crate::enumerate::list_directory(&self.root.0)
        } else {
            let dir = self
                .resolve_strict(&parts, crate::ffi::OpenKind::Directory)
                .map_err(map_error)?;
            crate::enumerate::list_directory(&dir)
        };
        let listed = listed.map_err(map_error)?;
        Ok(listed
            .into_iter()
            .map(|e| {
                (
                    e.name,
                    e.metadata.type_label().to_string(),
                    e.metadata.size,
                    e.metadata.last_write_100ns,
                )
            })
            .collect())
    }

    /// Open-and-close one directory to surface PATH/NOT_DIRECTORY before
    /// channel work (the v0.9 find/search pre-open contract).
    fn validate_directory(&self, parts: Vec<String>) -> Result<(), PyErr> {
        if parts.is_empty() {
            return Ok(()); // the retained root was validated at open
        }
        self.resolve_strict(&parts, crate::ffi::OpenKind::Directory)
            .map(|_validated_and_dropped| ())
            .map_err(map_error)
    }

    /// Paginated UTF-8 text read with before/after revision stability;
    /// returns (revision, lines, bytes_returned, end_line, has_more,
    /// has_nul, has_bom) for the backend-neutral TextPage contract.
    #[allow(clippy::too_many_arguments)]
    fn read_text_page(
        &self,
        parts: Vec<String>,
        start_line: u64,
        max_lines: u64,
        max_read_bytes: u64,
        binary_sample: u64,
    ) -> Result<PageTuple, PyErr> {
        if parts.is_empty() {
            return Err(map_error(NativeError::IsADirectory));
        }
        let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
        let page = crate::read::read_text_page(
            &self.root.0,
            &refs,
            start_line,
            max_lines,
            max_read_bytes,
            binary_sample,
        )
        .map_err(map_error)?;
        Ok((
            page.revision,
            page.lines,
            page.bytes_returned,
            page.end_line,
            page.has_more,
            page.has_nul,
            page.has_bom,
        ))
    }

    /// Bounded whole-file read with integrity: (data, sha256, revision).
    /// The consistency transaction and hash are computed in the kernel.
    fn read_bounded(
        &self,
        parts: Vec<String>,
        max_bytes: u64,
    ) -> Result<(Vec<u8>, String, String), PyErr> {
        if parts.is_empty() {
            return Err(map_error(NativeError::IsADirectory));
        }
        let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
        let read = crate::read::read_bounded(&self.root.0, &refs, max_bytes).map_err(map_error)?;
        let sha = crate::read::sha256_hex(&read.data);
        Ok((read.data, sha, read.metadata.revision()))
    }
}

impl NativeWorkdirSession {
    /// Resolve `parts` strictly (reparse refused on every component,
    /// empty = the retained root itself) and run `body` on the handle.
    fn with_target<T>(
        &self,
        parts: &[String],
        body: impl FnOnce(&crate::Handle) -> Result<T, NativeError>,
    ) -> Result<T, PyErr> {
        if parts.is_empty() {
            return body(&self.root.0).map_err(map_error);
        }
        let handle = self.resolve_for_report_handle(parts).map_err(map_error)?;
        body(&handle).map_err(map_error)
    }

    fn resolve_for_report_handle(&self, parts: &[String]) -> Result<crate::Handle, NativeError> {
        let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
        traversal::resolve_for_report(&self.root.0, &refs)
    }

    fn resolve_strict(
        &self,
        parts: &[String],
        kind: crate::ffi::OpenKind,
    ) -> Result<crate::Handle, NativeError> {
        let refs: Vec<&str> = parts.iter().map(String::as_str).collect();
        traversal::resolve(&self.root.0, &refs, kind)
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
