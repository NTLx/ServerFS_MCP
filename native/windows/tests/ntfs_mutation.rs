//! Windows 11 local-NTFS acceptance tests for Phase D1 mutations.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use serverfs_windows_native::{error::NativeError, ffi, metadata, mutation, traversal};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

struct Sandbox {
    root: PathBuf,
}

impl Sandbox {
    fn new(tag: &str) -> Self {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "serverfs_native_mutation_{tag}_{}_{}",
            std::process::id(),
            n
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        Self { root }
    }

    fn path(&self) -> &str {
        self.root.to_str().unwrap()
    }

    fn open(&self) -> serverfs_windows_native::Handle {
        traversal::open_root_with_access(self.path(), ffi::DIR_MUTATION_ACCESS).unwrap()
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn names(root: &serverfs_windows_native::Handle) -> Vec<String> {
    let mut scan = ffi::DirectoryScan::new();
    let mut output = Vec::new();
    while let Some(batch) = scan.next_batch(root).unwrap() {
        output.extend(batch);
    }
    output
}

#[test]
fn create_mutations_resolve_root_one_level_and_deep_parents() {
    let sandbox = Sandbox::new("parents");
    let root = sandbox.open();
    std::fs::create_dir(sandbox.root.join("subdir")).unwrap();
    std::fs::create_dir_all(sandbox.root.join("a").join("b")).unwrap();

    for (parts, expected) in [
        (vec!["root.txt"], b"root".as_slice()),
        (vec!["subdir", "one.txt"], b"one".as_slice()),
        (vec!["a", "b", "deep.txt"], b"deep".as_slice()),
    ] {
        mutation::create_bytes(&root, &parts, expected).unwrap();
        let path = parts
            .iter()
            .fold(sandbox.root.clone(), |path, part| path.join(part));
        assert_eq!(std::fs::read(path).unwrap(), expected);
    }
}

#[test]
fn create_is_create_only_for_files_directories_and_reparse_points() {
    let sandbox = Sandbox::new("create_only");
    let root = sandbox.open();
    std::fs::write(sandbox.root.join("file"), b"original").unwrap();
    std::fs::create_dir(sandbox.root.join("directory")).unwrap();
    assert_eq!(
        mutation::create_bytes(&root, &["file"], b"replacement").unwrap_err(),
        NativeError::PathAlreadyExists
    );
    assert_eq!(
        mutation::create_bytes(&root, &["directory"], b"replacement").unwrap_err(),
        NativeError::PathAlreadyExists
    );
    assert_eq!(
        std::fs::read(sandbox.root.join("file")).unwrap(),
        b"original"
    );
    assert!(sandbox.root.join("directory").is_dir());
}

#[test]
fn create_and_replace_return_postpublication_revision_and_exact_bytes() {
    let sandbox = Sandbox::new("replace");
    let root = sandbox.open();
    let before = mutation::create_bytes(&root, &["value.bin"], b"first\0bytes").unwrap();
    let after = mutation::replace_bytes(&root, &["value.bin"], b"second\0bytes", &before).unwrap();
    assert_ne!(before, after);
    assert_eq!(
        std::fs::read(sandbox.root.join("value.bin")).unwrap(),
        b"second\0bytes"
    );
    assert_eq!(
        metadata::collect(&traversal::resolve(&root, &["value.bin"], ffi::OpenKind::File).unwrap())
            .unwrap()
            .revision(),
        after
    );
}

#[test]
fn stale_revision_refuses_replacement_and_cleans_temp() {
    let sandbox = Sandbox::new("stale");
    let root = sandbox.open();
    let revision = mutation::create_bytes(&root, &["value"], b"old").unwrap();
    std::fs::write(sandbox.root.join("value"), b"host edit").unwrap();
    assert_eq!(
        mutation::replace_bytes(&root, &["value"], b"new", &revision).unwrap_err(),
        NativeError::RevisionConflict
    );
    assert_eq!(
        std::fs::read(sandbox.root.join("value")).unwrap(),
        b"host edit"
    );
    assert!(!names(&root)
        .iter()
        .any(|name| name.starts_with(mutation::INTERNAL_TEMP_PREFIX)));
}

#[test]
fn replacement_refuses_hardlinks_and_named_alternate_streams() {
    let sandbox = Sandbox::new("unsupported");
    let root = sandbox.open();
    let hard = sandbox.root.join("hard");
    std::fs::write(&hard, b"linked").unwrap();
    std::fs::hard_link(&hard, sandbox.root.join("hard-link")).unwrap();
    let hard_rev =
        metadata::revision_of(&traversal::resolve(&root, &["hard"], ffi::OpenKind::File).unwrap())
            .unwrap();
    assert_eq!(
        mutation::replace_bytes(&root, &["hard"], b"new", &hard_rev).unwrap_err(),
        NativeError::MultipleHardlinksNotSupported
    );

    let ads = sandbox.root.join("ads");
    std::fs::write(&ads, b"main").unwrap();
    std::fs::write(format!("{}:metadata", ads.display()), b"alternate").unwrap();
    let ads_rev =
        metadata::revision_of(&traversal::resolve(&root, &["ads"], ffi::OpenKind::File).unwrap())
            .unwrap();
    assert_eq!(
        mutation::replace_bytes(&root, &["ads"], b"new", &ads_rev).unwrap_err(),
        NativeError::MetadataPreservationFailed
    );
    assert_eq!(std::fs::read(ads).unwrap(), b"main");
}

#[test]
fn replacement_refuses_sparse_ntfs_state_before_touching_destination() {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_OPEN_REPARSE_POINT, OPEN_EXISTING,
    };
    use windows_sys::Win32::System::Ioctl::{FILE_SET_SPARSE_BUFFER, FSCTL_SET_SPARSE};
    use windows_sys::Win32::System::IO::DeviceIoControl;

    let sandbox = Sandbox::new("sparse");
    let root = sandbox.open();
    mutation::create_bytes(&root, &["sparse.bin"], b"sparse fixture").unwrap();
    let path = sandbox.root.join("sparse.bin");
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let raw = unsafe {
        CreateFileW(
            wide.as_ptr(),
            ffi::FILE_WRITE_DATA_ACCESS | ffi::FILE_READ_ATTRIBUTES_ACCESS,
            7,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    assert_ne!(
        raw,
        INVALID_HANDLE_VALUE,
        "open sparse fixture: {}",
        unsafe { GetLastError() }
    );
    let input = FILE_SET_SPARSE_BUFFER { SetSparse: true };
    let mut returned = 0;
    let result = unsafe {
        DeviceIoControl(
            raw,
            FSCTL_SET_SPARSE,
            (&input as *const FILE_SET_SPARSE_BUFFER).cast(),
            std::mem::size_of::<FILE_SET_SPARSE_BUFFER>() as u32,
            std::ptr::null_mut(),
            0,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    assert_ne!(result, 0, "set NTFS sparse state: {}", unsafe {
        GetLastError()
    });
    unsafe { CloseHandle(raw) };

    let sparse = traversal::resolve(&root, &["sparse.bin"], ffi::OpenKind::File).unwrap();
    let revision = metadata::revision_of(&sparse).unwrap();
    assert_ne!(metadata::collect(&sparse).unwrap().attributes & 0x200, 0);
    assert_eq!(
        mutation::replace_bytes(&root, &["sparse.bin"], b"replacement", &revision).unwrap_err(),
        NativeError::MetadataPreservationFailed
    );
    assert_eq!(std::fs::read(path).unwrap(), b"sparse fixture");
}

#[test]
fn plain_ntfs_file_and_ordinary_security_metadata_are_preserved() {
    let sandbox = Sandbox::new("security");
    let root = sandbox.open();
    let revision = mutation::create_bytes(&root, &["plain"], b"before").unwrap();
    let original = traversal::resolve(&root, &["plain"], ffi::OpenKind::File).unwrap();
    let original_security = ffi::security_descriptor(&original).unwrap();
    let original_sids = original_security.owner_group().unwrap();
    let original_basic = ffi::basic_info(&original).unwrap();
    mutation::replace_bytes(&root, &["plain"], b"after", &revision).unwrap();
    let replaced = traversal::resolve(&root, &["plain"], ffi::OpenKind::File).unwrap();
    let replaced_security = ffi::security_descriptor(&replaced).unwrap();
    assert_eq!(replaced_security.owner_group().unwrap(), original_sids);
    // Semantic preservation, not raw byte identity: an inherited DACL that
    // is re-applied as explicit DACL material legitimately loses NTFS'
    // AUTO_INHERITED control marker (measured on WorkPC %TEMP%).
    assert!(replaced_security.equivalent(&original_security));
    let replaced_basic = ffi::basic_info(&replaced).unwrap();
    assert_eq!(replaced_basic.CreationTime, original_basic.CreationTime);
    assert_eq!(replaced_basic.LastAccessTime, original_basic.LastAccessTime);
    assert_eq!(replaced_basic.LastWriteTime, original_basic.LastWriteTime);
    assert_eq!(replaced_basic.FileAttributes, original_basic.FileAttributes);
    assert_eq!(std::fs::read(sandbox.root.join("plain")).unwrap(), b"after");
}

#[test]
fn raw_directory_scan_sees_internal_temp_but_tool_enumeration_hides_it() {
    let sandbox = Sandbox::new("temp_hidden");
    let root = sandbox.open();
    let internal = format!("{}test.tmp", mutation::INTERNAL_TEMP_PREFIX);
    std::fs::write(sandbox.root.join(&internal), b"temporary").unwrap();
    assert!(names(&root).contains(&internal));
    let listed = serverfs_windows_native::enumerate::list_directory(&root).unwrap();
    assert!(!listed.iter().any(|entry| entry.name == internal));

    mutation::create_directory(&root, &["empty-check"]).unwrap();
    std::fs::write(
        sandbox.root.join("empty-check").join(&internal),
        b"reserved physical entry",
    )
    .unwrap();
    let dir = traversal::resolve(&root, &["empty-check"], ffi::OpenKind::Directory).unwrap();
    let dir_revision = metadata::revision_of(&dir).unwrap();
    assert_eq!(
        mutation::delete_directory(&root, &["empty-check"], &dir_revision).unwrap_err(),
        NativeError::DirectoryNotEmpty
    );
}

#[test]
fn delete_and_directory_mutations_are_revision_guarded_and_nonrecursive() {
    let sandbox = Sandbox::new("delete_mkdir");
    let root = sandbox.open();
    let file_rev = mutation::create_bytes(&root, &["delete-me"], b"file").unwrap();
    assert_eq!(
        mutation::delete_file(&root, &["delete-me"], "v1:0000000000000000").unwrap_err(),
        NativeError::RevisionConflict
    );
    assert!(sandbox.root.join("delete-me").exists());
    mutation::delete_file(&root, &["delete-me"], &file_rev).unwrap();
    assert!(!sandbox.root.join("delete-me").exists());

    let dir_rev = mutation::create_directory(&root, &["new-dir"]).unwrap();
    assert_eq!(
        mutation::create_directory(&root, &["new-dir"]).unwrap_err(),
        NativeError::PathAlreadyExists
    );
    std::fs::write(sandbox.root.join("new-dir").join("child"), b"x").unwrap();
    // Platform semantics, measured on WorkPC NTFS: unlike the POSIX line,
    // adding a child does NOT move a directory's LastWriteTime, so a child
    // write cannot make a stored directory revision stale here. Produce
    // the staleness the same way an explicit metadata change would.
    let touch = traversal::open_component_with_access(
        &root,
        "new-dir",
        ffi::OpenKind::Directory,
        // READ_ATTRIBUTES is required by the traversal validator on the
        // handle it just opened, on top of the writer right under test.
        ffi::FILE_WRITE_ATTRIBUTES_ACCESS | ffi::FILE_READ_ATTRIBUTES_ACCESS,
    )
    .unwrap();
    let mut current_basic = ffi::basic_info(&touch).unwrap();
    current_basic.LastWriteTime += 10_000_000;
    ffi::set_basic(&touch, &current_basic).unwrap();
    drop(touch);
    assert_eq!(
        mutation::delete_directory(&root, &["new-dir"], &dir_rev).unwrap_err(),
        NativeError::RevisionConflict
    );
    let nonempty_dir_revision = metadata::revision_of(
        &traversal::resolve(&root, &["new-dir"], ffi::OpenKind::Directory).unwrap(),
    )
    .unwrap();
    assert_eq!(
        mutation::delete_directory(&root, &["new-dir"], &nonempty_dir_revision).unwrap_err(),
        NativeError::DirectoryNotEmpty
    );
    std::fs::remove_file(sandbox.root.join("new-dir").join("child")).unwrap();
    let empty_dir_revision = metadata::revision_of(
        &traversal::resolve(&root, &["new-dir"], ffi::OpenKind::Directory).unwrap(),
    )
    .unwrap();
    mutation::delete_directory(&root, &["new-dir"], &empty_dir_revision).unwrap();
    assert!(!sandbox.root.join("new-dir").exists());
}

#[test]
fn mutations_continue_through_retained_root_after_host_root_rename() {
    let mut sandbox = Sandbox::new("root_renamed");
    let root = sandbox.open();
    let moved = sandbox.root.with_extension("moved");
    std::fs::rename(&sandbox.root, &moved).unwrap();
    sandbox.root = moved.clone();
    mutation::create_bytes(&root, &["still-rooted"], b"works").unwrap();
    assert_eq!(std::fs::read(moved.join("still-rooted")).unwrap(), b"works");
}

#[test]
fn mutation_handle_count_stays_bounded_across_success_and_failure_paths() {
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessHandleCount};

    let sandbox = Sandbox::new("handle_stress");
    let root = sandbox.open();
    let mut before = 0;
    assert_ne!(
        unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut before) },
        0
    );
    for index in 0..75 {
        let file = format!("file-{index}");
        let revision = mutation::create_bytes(&root, &[&file], b"one").unwrap();
        let replacement = mutation::replace_bytes(&root, &[&file], b"two", &revision).unwrap();
        assert_eq!(
            mutation::create_bytes(&root, &[&file], b"collision").unwrap_err(),
            NativeError::PathAlreadyExists
        );
        assert!(!names(&root)
            .iter()
            .any(|name| name.starts_with(mutation::INTERNAL_TEMP_PREFIX)));
        assert_eq!(
            mutation::delete_file(&root, &[&file], &revision).unwrap_err(),
            NativeError::RevisionConflict
        );
        mutation::delete_file(&root, &[&file], &replacement).unwrap();

        let directory = format!("directory-{index}");
        let directory_revision = mutation::create_directory(&root, &[&directory]).unwrap();
        assert_eq!(
            mutation::create_directory(&root, &[&directory]).unwrap_err(),
            NativeError::PathAlreadyExists
        );
        mutation::delete_directory(&root, &[&directory], &directory_revision).unwrap();
    }
    let mut after = 0;
    assert_ne!(
        unsafe { GetProcessHandleCount(GetCurrentProcess(), &mut after) },
        0
    );
    assert!(
        after <= before + 1,
        "handle count grew from {before} to {after}"
    );
}

#[test]
fn concurrent_readers_never_observe_partial_replacement() {
    let sandbox = Sandbox::new("concurrent");
    let root = sandbox.open();
    let first = vec![b'a'; 1024 * 1024];
    let second = vec![b'b'; 1024 * 1024];
    let mut revision = mutation::create_bytes(&root, &["large.bin"], &first).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let reader_stop = Arc::clone(&stop);
    let stable_reads = Arc::new(AtomicUsize::new(0));
    let reader_stable_reads = Arc::clone(&stable_reads);
    let root_path = sandbox.root.clone();
    let first_reader = first.clone();
    let second_reader = second.clone();
    let reader = std::thread::spawn(move || {
        let root_path = root_path.to_str().unwrap();
        let read_root = traversal::open_root(root_path).unwrap();
        while !reader_stop.load(Ordering::Relaxed) {
            let read = serverfs_windows_native::read::read_bounded(
                &read_root,
                &["large.bin"],
                2 * 1024 * 1024,
            );
            match read {
                Ok(read) => {
                    assert!(read.data == first_reader || read.data == second_reader);
                    reader_stable_reads.fetch_add(1, Ordering::Relaxed);
                }
                // The read API refuses to return bytes when its revision
                // changes during the read transaction.
                Err(NativeError::ChangedDuringRead) => {}
                Err(error) => panic!("unexpected concurrent read error: {error:?}"),
            }
        }
    });
    for index in 0..50 {
        let data = if index % 2 == 0 { &second } else { &first };
        revision = mutation::replace_bytes(&root, &["large.bin"], data, &revision).unwrap();
    }
    stop.store(true, Ordering::Relaxed);
    reader.join().unwrap();
    assert!(stable_reads.load(Ordering::Relaxed) > 0);
}

#[test]
fn required_symlink_cases_are_executed_by_windows_ci() {
    use std::os::windows::fs::{symlink_dir, symlink_file};

    let sandbox = Sandbox::new("reparse");
    let root = sandbox.open();
    let external = sandbox.root.with_extension("external");
    std::fs::create_dir(&external).unwrap();
    std::fs::write(external.join("outside"), b"safe").unwrap();
    let file_link = sandbox.root.join("file-link");
    let dir_link = sandbox.root.join("dir-link");
    let junction = sandbox.root.join("junction");
    let links_created = symlink_file(external.join("outside"), &file_link).is_ok()
        && symlink_dir(&external, &dir_link).is_ok()
        && std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(&junction)
            .arg(&external)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| status.success());
    if !links_created {
        assert_ne!(
            std::env::var("SERVERFS_REQUIRE_SYMLINK").as_deref(),
            Ok("1")
        );
        return;
    }
    let file_err = mutation::replace_bytes(&root, &["file-link"], b"x", "unused").unwrap_err();
    assert_eq!(file_err, NativeError::ReparsePoint);
    assert_eq!(
        mutation::create_bytes(&root, &["file-link"], b"must not follow").unwrap_err(),
        NativeError::PathAlreadyExists
    );
    assert_eq!(
        mutation::create_bytes(&root, &["dir-link"], b"must not follow").unwrap_err(),
        NativeError::PathAlreadyExists
    );
    assert_eq!(
        mutation::create_bytes(&root, &["junction"], b"must not follow").unwrap_err(),
        NativeError::PathAlreadyExists
    );
    assert_eq!(
        mutation::delete_directory(&root, &["dir-link"], "unused").unwrap_err(),
        NativeError::ReparsePoint
    );
    assert_eq!(
        mutation::delete_directory(&root, &["junction"], "unused").unwrap_err(),
        NativeError::ReparsePoint
    );
    assert_eq!(std::fs::read(external.join("outside")).unwrap(), b"safe");
    assert!(Path::new(&external).exists());
}

#[test]
fn held_mutation_target_share_blocks_ordinary_external_mutation_opens() {
    // Windows sharing-mode defense-in-depth evidence against ordinary
    // Win32 writers (dev_plan §19.4): while the mutation kernel holds a
    // delete-style target open with FILE_SHARE_READ, new ordinary WRITE
    // opens and new DELETE opens (both plain deletion and path-based
    // rename require DELETE access) are refused, while ServerFS read
    // channels keep serving the object. After the handle is released the
    // same operations succeed.
    //
    // This does NOT prove atomic compare-and-swap against every hostile
    // same-user process or every POSIX-style rename primitive; the
    // name-relative final gate is what detects drift those paths cause.
    let sandbox = Sandbox::new("held_share");
    let root = sandbox.open();
    let target = sandbox.root.join("h.txt");
    std::fs::write(&target, b"payload").unwrap();
    let held = traversal::open_component_with_share(
        &root,
        "h.txt",
        ffi::OpenKind::File,
        ffi::FILE_DELETE_ACCESS | ffi::FILE_READ_ATTRIBUTES_ACCESS | ffi::SYNCHRONIZE_ACCESS,
        ffi::FILE_SHARE_READ,
    )
    .expect("mutation-style held open");

    // Ordinary external programs are simulated with path-based Win32
    // APIs here deliberately: that is the test stimulus, not the
    // production kernel.
    let write_open = std::fs::OpenOptions::new().write(true).open(&target);
    assert_eq!(
        write_open.err().and_then(|e| e.raw_os_error()),
        Some(32), // ERROR_SHARING_VIOLATION
        "new external WRITE must be refused while the target is held"
    );
    let rename = std::fs::rename(&target, sandbox.root.join("moved.txt"));
    assert_eq!(
        rename.err().and_then(|e| e.raw_os_error()),
        Some(32),
        "ordinary external rename must be refused while the target is held"
    );
    let delete = std::fs::remove_file(&target);
    assert_eq!(
        delete.err().and_then(|e| e.raw_os_error()),
        Some(32),
        "ordinary external delete must be refused while the target is held"
    );

    // The read channels stay compatible with the held restrictive share.
    let read = serverfs_windows_native::read::read_bounded(&root, &["h.txt"], 64).unwrap();
    assert_eq!(read.data, b"payload");

    drop(held);
    std::fs::OpenOptions::new()
        .write(true)
        .open(&target)
        .expect("WRITE open succeeds after release")
        .set_len(0)
        .unwrap();
    std::fs::rename(&target, sandbox.root.join("moved.txt")).expect("rename after release");
    std::fs::remove_file(sandbox.root.join("moved.txt")).expect("delete after release");
    assert!(!target.exists());
}
