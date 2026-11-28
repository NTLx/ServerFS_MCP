//! Real-NTFS acceptance for the §13 traversal primitive.
//!
//! These tests run against the actual Windows kernel on the build host —
//! mocked Win32 is explicitly not sufficient (dev_plan §29.5). A volume
//! without `FILE_ID_INFO` support fails identity assertions, which is the
//! intended signal that the sandbox is not on an NTFS-class filesystem.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use serverfs_windows_native::error::NativeError;
use serverfs_windows_native::ffi::OpenKind;
use serverfs_windows_native::{metadata, traversal};

struct Sandbox {
    root: PathBuf,
}

static COUNTER: AtomicUsize = AtomicUsize::new(0);

impl Sandbox {
    fn new(tag: &str) -> Sandbox {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("serverfs_native_{tag}_{}_{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create sandbox");
        Sandbox { root }
    }

    fn path(&self) -> &str {
        self.root.to_str().expect("utf-8 temp path")
    }

    fn write(&self, rel: &str) -> PathBuf {
        let p = self.root.join(rel);
        if let Some(dir) = p.parent() {
            std::fs::create_dir_all(dir).unwrap();
        }
        std::fs::write(&p, b"payload\n").unwrap();
        p
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn symlink_supported(result: &std::io::Result<()>) -> bool {
    match result {
        Err(e) if e.raw_os_error() == Some(1314) => {
            // the dedicated Windows acceptance CI job sets
            // SERVERFS_REQUIRE_SYMLINK=1, where an unexercised symlink
            // case is a hard failure rather than a soft skip
            if std::env::var("SERVERFS_REQUIRE_SYMLINK").is_ok() {
                panic!(
                    "SERVERFS_REQUIRE_SYMLINK=1 but symlink creation lacks \
                     SeCreateSymbolicLinkPrivilege/Developer Mode"
                );
            }
            eprintln!(
                "SKIPPED (privilege not held): symlink reparse coverage needs \
                       SeCreateSymbolicLinkPrivilege or Developer Mode"
            );
            false
        }
        Ok(_) => true,
        Err(e) => panic!("symlink creation failed unexpectedly: {e}"),
    }
}

/// Junctions (IO_REPARSE_TAG_MOUNT_POINT) are reparse points that ordinary
/// users may create without privileges — they carry the traversal proof for
/// the reparse refusal invariant even when symlinks cannot be created.
fn make_junction(link: &std::path::Path, target: &std::path::Path) -> bool {
    let status = std::process::Command::new("cmd")
        .args([
            "/C",
            "mklink",
            "/J",
            link.to_str().unwrap(),
            target.to_str().unwrap(),
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
    let ok = matches!(status, Ok(s) if s.success());
    if !ok && std::env::var("SERVERFS_REQUIRE_SYMLINK").is_ok() {
        panic!("SERVERFS_REQUIRE_SYMLINK=1 but junction creation failed");
    }
    ok
}

#[test]
fn junction_reparse_point_is_refused_and_never_followed() {
    let sandbox = Sandbox::new("junction");
    sandbox.write("real/inner.txt");
    let link = sandbox.root.join("jlink");
    if !make_junction(&link, &sandbox.root.join("real")) {
        eprintln!("SKIPPED: could not create a junction with mklink /J");
        return;
    }

    let root = traversal::open_root(sandbox.path()).unwrap();
    // The junction names an object that is a reparse point: it must be
    // refused AS AN OBJECT, never traversed into its target.
    assert_eq!(
        traversal::open_component(&root, "jlink", OpenKind::Directory).err(),
        Some(NativeError::ReparsePoint)
    );
    assert_eq!(
        traversal::open_component(&root, "jlink", OpenKind::Any).err(),
        Some(NativeError::ReparsePoint)
    );
    assert_eq!(
        traversal::resolve(&root, &["jlink", "inner.txt"], OpenKind::File).err(),
        Some(NativeError::ReparsePoint)
    );
    // The real directory remains reachable through its true name.
    traversal::resolve(&root, &["real", "inner.txt"], OpenKind::File).expect("real path works");
}

#[test]
fn root_open_rejects_missing_files_relative_paths_and_non_directories() {
    let sandbox = Sandbox::new("rootopen");
    let file = sandbox.write("plain.txt");

    assert_eq!(
        traversal::open_root(sandbox.root.join("absent").to_str().unwrap()).err(),
        Some(NativeError::PathNotFound)
    );
    assert!(matches!(
        traversal::open_root("relative/path"),
        Err(NativeError::InvalidRoot)
    ));
    assert_eq!(
        traversal::open_root(file.to_str().unwrap()).err(),
        Some(NativeError::NotADirectory)
    );

    let root = traversal::open_root(sandbox.path()).expect("open root");
    assert!(metadata::is_directory(&root).unwrap());
}

#[test]
fn handle_relative_resolution_and_identity_survive_external_rename() {
    let sandbox = Sandbox::new("identity");
    sandbox.write("a/b/c.txt");
    let root = traversal::open_root(sandbox.path()).unwrap();

    let handle =
        traversal::resolve(&root, &["a", "b", "c.txt"], OpenKind::File).expect("resolve c.txt");
    let id_before = metadata::object_id(&handle).unwrap();

    // External rename (the trusted-name channel, as an IDE or host user
    // would do): the retained handle keeps naming the same object.
    std::fs::rename(
        sandbox.root.join("a/b/c.txt"),
        sandbox.root.join("a/b/d.txt"),
    )
    .unwrap();
    let id_still = metadata::object_id(&handle).unwrap();
    assert_eq!(
        id_before, id_still,
        "held handle identity changed under rename"
    );

    // The new name resolves to the very same object id: identity is the
    // object, not the pathname.
    let reopened =
        traversal::resolve(&root, &["a", "b", "d.txt"], OpenKind::File).expect("resolve d.txt");
    assert_eq!(id_before, metadata::object_id(&reopened).unwrap());

    // The old name is gone for later resolutions.
    assert_eq!(
        traversal::resolve(&root, &["a", "b", "c.txt"], OpenKind::File).err(),
        Some(NativeError::PathNotFound)
    );
}

#[test]
fn workdir_root_that_is_itself_a_junction_fails_closed() {
    let sandbox = Sandbox::new("rootjunction");
    sandbox.write("real/inner.txt");
    let link = sandbox.root.join("jroot");
    if !make_junction(&link, &sandbox.root.join("real")) {
        eprintln!("SKIPPED: could not create a junction with mklink /J");
        return;
    }

    // §10/§13: the trusted anchor itself being a reparse point must be
    // refused from the handle's own attributes, never opened through.
    assert_eq!(
        traversal::open_root(link.to_str().unwrap()).err(),
        Some(NativeError::ReparsePoint)
    );
}

#[test]
fn symlink_directory_is_refused_and_never_followed() {
    let sandbox = Sandbox::new("symlinkdir");
    sandbox.write("real/inner.txt");
    let link = sandbox.root.join("link");
    let made = std::os::windows::fs::symlink_dir(sandbox.root.join("real"), &link);
    if !symlink_supported(&made) {
        return;
    }

    let root = traversal::open_root(sandbox.path()).unwrap();
    // Traversing THROUGH the link must fail on the link component itself:
    // it is opened as itself (FILE_OPEN_REPARSE_POINT), never followed.
    assert_eq!(
        traversal::resolve(&root, &["link", "inner.txt"], OpenKind::File).err(),
        Some(NativeError::ReparsePoint)
    );
    assert_eq!(
        traversal::open_component(&root, "link", OpenKind::Directory).err(),
        Some(NativeError::ReparsePoint)
    );
    // The real target remains reachable through its true name.
    traversal::resolve(&root, &["real", "inner.txt"], OpenKind::File).expect("real path works");
}

#[test]
fn symlink_file_is_refused() {
    let sandbox = Sandbox::new("symlinkfile");
    sandbox.write("target.txt");
    let link = sandbox.root.join("alias.txt");
    let made = std::os::windows::fs::symlink_file(sandbox.root.join("target.txt"), &link);
    if !symlink_supported(&made) {
        return;
    }

    let root = traversal::open_root(sandbox.path()).unwrap();
    assert_eq!(
        traversal::resolve(&root, &["alias.txt"], OpenKind::File).err(),
        Some(NativeError::ReparsePoint)
    );
    traversal::resolve(&root, &["target.txt"], OpenKind::File).expect("real file works");
}

#[test]
fn type_expectations_are_enforced_on_the_same_open() {
    let sandbox = Sandbox::new("types");
    sandbox.write("dir/sub.txt");
    sandbox.write("plain.txt");
    let root = traversal::open_root(sandbox.path()).unwrap();

    // Directory where a file is expected: FILE_NON_DIRECTORY_FILE refuses.
    assert_eq!(
        traversal::resolve(&root, &["dir"], OpenKind::File).err(),
        Some(NativeError::IsADirectory)
    );
    // File where a directory is expected: FILE_DIRECTORY_FILE refuses.
    assert_eq!(
        traversal::resolve(&root, &["plain.txt"], OpenKind::Directory).err(),
        Some(NativeError::NotADirectory)
    );
    // A file used as an intermediate component fails at that component.
    assert_eq!(
        traversal::resolve(&root, &["plain.txt", "x"], OpenKind::Any).err(),
        Some(NativeError::NotADirectory)
    );
}

#[test]
fn kernel_name_gate_refuses_traversal_and_ambiguity_before_syscall() {
    let sandbox = Sandbox::new("names");
    let root = traversal::open_root(sandbox.path()).unwrap();

    let mut bad_names: Vec<String> = [
        "",
        ".",
        "..",
        "a\\b",
        "a/b",
        "a:b",
        "a\0b",
        "trailing.",
        "trailing ",
    ]
    .iter()
    .map(|s| (*s).to_string())
    .collect();
    bad_names.push("x".repeat(300));
    for bad in &bad_names {
        assert_eq!(
            traversal::open_component(&root, bad, OpenKind::Any).err(),
            Some(NativeError::InvalidName),
            "kernel must refuse {bad:?}"
        );
    }
    assert_eq!(
        traversal::resolve(&root, &["..", "Windows"], OpenKind::Any).err(),
        Some(NativeError::InvalidName)
    );
}

#[test]
fn no_handle_leak_across_stress_and_failure_paths() {
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessHandleCount};

    fn handle_count() -> u32 {
        let mut count = 0u32;
        let ok = unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut count) };
        assert_ne!(ok, 0, "GetProcessHandleCount failed");
        count
    }

    let sandbox = Sandbox::new("leak");
    sandbox.write("f.txt");
    let root = traversal::open_root(sandbox.path()).unwrap();
    let baseline = handle_count();

    for i in 0..2000 {
        // successful relative opens
        let _ = traversal::resolve(&root, &["f.txt"], OpenKind::File).unwrap();
        // failure paths must also be leak-free
        let missing = format!("missing{i}");
        let _ = traversal::resolve(&root, &[missing.as_str()], OpenKind::File);
        let _ = traversal::resolve(&root, &["f.txt", "x"], OpenKind::Any);
    }

    let after = handle_count();
    // Windows allocates transient handles internally; allow a small margin.
    assert!(
        after.abs_diff(baseline) <= 16,
        "handle count moved from {baseline} to {after} — leak in open or error paths"
    );
}
