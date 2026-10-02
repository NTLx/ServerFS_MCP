//! Real-NTFS acceptance for the Phase D1 create-only mutation kernel.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use serverfs_windows_native::{metadata, mutation, traversal, NativeError};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

struct Sandbox(PathBuf);

impl Sandbox {
    fn new() -> Self {
        let id = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "serverfs_native_mutation_{}_{}",
            std::process::id(),
            id
        ));
        std::fs::create_dir_all(&path).expect("create NTFS sandbox");
        Self(path)
    }

    fn root_handle(&self) -> serverfs_windows_native::Handle {
        traversal::open_root_with_create_access(
            self.0.to_str().expect("UTF-8 sandbox path"),
            true,
        )
        .expect("open retained root")
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn create_file_and_directory_are_handle_relative_and_create_only() {
    let sandbox = Sandbox::new();
    let root = sandbox.root_handle();
    let revision = mutation::create_file(&root, &["created.txt"], b"complete")
        .unwrap_or_else(|err| panic!("create file failed: {err:?}"));
    assert_eq!(std::fs::read(sandbox.0.join("created.txt")).unwrap(), b"complete");
    let created = traversal::resolve(
        &root,
        &["created.txt"],
        serverfs_windows_native::ffi::OpenKind::File,
    )
    .expect("resolve created file");
    assert_eq!(metadata::revision_of(&created).unwrap(), revision);
    assert_eq!(
        mutation::create_file(&root, &["created.txt"], b"replacement"),
        Err(NativeError::AlreadyExists)
    );
    assert_eq!(std::fs::read(sandbox.0.join("created.txt")).unwrap(), b"complete");

    let directory_revision = mutation::create_directory(&root, &["created-dir"])
        .unwrap_or_else(|err| panic!("create directory failed: {err:?}"));
    let directory = traversal::resolve(
        &root,
        &["created-dir"],
        serverfs_windows_native::ffi::OpenKind::Directory,
    )
    .expect("resolve created directory");
    assert_eq!(metadata::revision_of(&directory).unwrap(), directory_revision);
    assert_eq!(
        mutation::create_directory(&root, &["created-dir"]),
        Err(NativeError::AlreadyExists)
    );
    assert_eq!(
        std::fs::read_dir(&sandbox.0)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().starts_with(".serverfs-tmp-"))
            .count(),
        0
    );
}
