//! ServerFS Windows native filesystem kernel (v0.10 Phase B).
//!
//! This crate is the HANDLE-based counterpart of the Linux `fdio` kernel.
//! Security model (dev_plan_v0.10.md §13), mirroring the proven Linux
//! design rather than its syntax:
//!
//! - the workdir root is the one object opened by name (a trusted anchor
//!   from configuration, never request-derived);
//! - every request-derived component is opened relative to an already
//!   trusted parent directory HANDLE via `NtCreateFile`
//!   (`OBJECT_ATTRIBUTES.RootDirectory`) — there is no path-string
//!   resolution step to race;
//! - reparse points are never followed: every open carries
//!   `FILE_OPEN_REPARSE_POINT`, and the returned handle's own attributes
//!   are checked, so a symlink/junction is refused as an object, not
//!   traversed through;
//! - type expectations (`FILE_DIRECTORY_FILE` / `FILE_NON_DIRECTORY_FILE`)
//!   are enforced by the kernel on the same open, and identity
//!   (`FILE_ID_INFO`) is always read from the held handle;
//! - raw FFI calls live only in [`ffi`] (plus the one ownership-guaranteed
//!   `CloseHandle` in [`handle`]); a successfully returned HANDLE is
//!   immediately owned by the RAII [`Handle`], which closes it on every
//!   path including panics;
//! - no thread-mobility contract is asserted on [`Handle`] yet: Send/Sync
//!   must be proven per handle role when the session model needs it.
//!
//! This module currently proves the §13 traversal primitive. Enumeration,
//! read, search and mutation kernels are additive follow-ups in the same
//! shape. No Python boundary exists in this crate yet; handles must never
//! be exposed past it when the PyO3 layer lands.

pub mod error;
pub mod ffi;
pub mod handle;
pub mod metadata;
pub mod path;
pub mod traversal;

pub use error::NativeError;
pub use handle::Handle;
