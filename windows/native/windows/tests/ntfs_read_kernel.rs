//! Real-NTFS probes for the Phase B read kernel (§15/§16).
//!
//! The revision material tuple was frozen by running these probes: they
//! pin the properties the review requires — content moves the revision,
//! metadata-only changes move it, renames keep it, same-name replacement
//! always moves it, and the token never carries raw identity.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use serverfs_windows_native::error::NativeError;
use serverfs_windows_native::ffi::OpenKind;
use serverfs_windows_native::metadata;
use serverfs_windows_native::read;
use serverfs_windows_native::traversal;

static COUNTER: AtomicUsize = AtomicUsize::new(0);

struct Sandbox {
    root: PathBuf,
}

impl Sandbox {
    fn new(tag: &str) -> Sandbox {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "serverfs_native_read_{tag}_{}_{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        Sandbox { root }
    }
    fn path(&self) -> &str {
        self.root.to_str().unwrap()
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn make_junction(link: &Path, target: &Path) -> bool {
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
    matches!(status, Ok(s) if s.success())
}

fn open(sandbox: &Sandbox) -> serverfs_windows_native::Handle {
    traversal::open_root(sandbox.path()).unwrap()
}

fn revision_of(root: &serverfs_windows_native::Handle, parts: &[&str]) -> String {
    let h = traversal::resolve_for_report(root, parts).unwrap();
    metadata::collect(&h).unwrap().revision()
}

#[test]
fn revision_is_stable_for_an_unchanged_object() {
    let sandbox = Sandbox::new("stab");
    std::fs::write(sandbox.root.join("a.txt"), b"one\n").unwrap();
    let root = open(&sandbox);
    assert_eq!(
        revision_of(&root, &["a.txt"]),
        revision_of(&root, &["a.txt"])
    );
}

#[test]
fn content_edit_moves_the_revision() {
    let sandbox = Sandbox::new("edit");
    let file = sandbox.root.join("a.txt");
    std::fs::write(&file, b"one\n").unwrap();
    let root = open(&sandbox);
    let before = revision_of(&root, &["a.txt"]);
    std::fs::write(&file, b"one two three\n").unwrap();
    assert_ne!(before, revision_of(&root, &["a.txt"]));
}

#[test]
fn metadata_only_attribute_change_moves_the_revision() {
    use windows_sys::Win32::Storage::FileSystem::SetFileAttributesW;
    const FILE_ATTRIBUTE_HIDDEN: u32 = 2;

    let sandbox = Sandbox::new("attrs");
    let file = sandbox.root.join("a.txt");
    std::fs::write(&file, b"same bytes\n").unwrap();
    let root = open(&sandbox);
    let before = revision_of(&root, &["a.txt"]);
    let wide: Vec<u16> = file
        .to_str()
        .unwrap()
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let ok = unsafe { SetFileAttributesW(wide.as_ptr(), FILE_ATTRIBUTE_HIDDEN) };
    assert_ne!(ok, 0, "SetFileAttributesW failed");
    assert_ne!(
        before,
        revision_of(&root, &["a.txt"]),
        "attributes are revision-relevant metadata"
    );
}

#[test]
fn rename_does_not_move_the_revision_of_the_object() {
    let sandbox = Sandbox::new("rename");
    let file = sandbox.root.join("before.txt");
    std::fs::write(&file, b"content\n").unwrap();
    let root = open(&sandbox);
    let before = revision_of(&root, &["before.txt"]);
    std::fs::rename(&file, sandbox.root.join("after.txt")).unwrap();
    let after = revision_of(&root, &["after.txt"]);
    assert_eq!(
        before, after,
        "the revision identifies the object; a rename alone must not move it \
         (probe result: NTFS rename does not touch the revision material tuple)"
    );
}

#[test]
fn same_name_replacement_always_moves_the_revision() {
    let sandbox = Sandbox::new("replace");
    let file = sandbox.root.join("a.txt");
    std::fs::write(&file, b"same\n").unwrap();
    let root = open(&sandbox);
    let first = revision_of(&root, &["a.txt"]);
    std::fs::remove_file(&file).unwrap();
    std::fs::write(&file, b"same\n").unwrap();
    let second = revision_of(&root, &["a.txt"]);
    assert_ne!(
        first, second,
        "a new object under the old name is a new revision"
    );
}

#[test]
fn revision_token_shape_never_exposes_raw_identity() {
    let sandbox = Sandbox::new("shape");
    std::fs::write(sandbox.root.join("a.txt"), b"x\n").unwrap();
    let root = open(&sandbox);
    let h = traversal::resolve_for_report(&root, &["a.txt"]).unwrap();
    let md = metadata::collect(&h).unwrap();
    let revision = md.revision();
    assert!(revision.starts_with("v1:"), "{revision}");
    let hex = &revision[3..];
    assert_eq!(hex.len(), 16);
    assert!(hex.chars().all(|c| c.is_ascii_hexdigit()));
    // the digest must not embed the decimal identity values it was built from
    let decimals = [
        md.id.volume_serial.to_string(),
        md.id.file_id.to_string(),
        md.size.unwrap_or(0).to_string(),
        md.link_count.to_string(),
    ];
    for value in decimals {
        if value.len() >= 4 {
            assert!(!hex.contains(&value), "revision leaks {value}");
        }
    }
}

#[test]
fn read_text_page_matches_the_linux_line_semantics() {
    let sandbox = Sandbox::new("page");
    let root = open(&sandbox);

    std::fs::write(sandbox.root.join("basic.txt"), b"l1\nl2\nl3\n").unwrap();
    let page = read::read_text_page(&root, &["basic.txt"], 1, 2, 4096, 1024).unwrap();
    assert_eq!(page.lines, vec![b"l1\n".to_vec(), b"l2\n".to_vec()]);
    assert!(page.has_more);
    assert_eq!(page.end_line, 2);
    assert_eq!(page.bytes_returned, 6);

    // last line without a terminator still counts
    std::fs::write(sandbox.root.join("nonl.txt"), b"a\nb").unwrap();
    let page = read::read_text_page(&root, &["nonl.txt"], 1, 10, 4096, 1024).unwrap();
    assert_eq!(page.lines, vec![b"a\n".to_vec(), b"b".to_vec()]);
    assert!(!page.has_more);

    // BOM stripped on line 1 only
    std::fs::write(sandbox.root.join("bom.txt"), b"\xEF\xBB\xBFhead\nnext\n").unwrap();
    let page = read::read_text_page(&root, &["bom.txt"], 1, 10, 4096, 1024).unwrap();
    assert!(page.has_bom);
    assert_eq!(page.lines[0], b"head\n".to_vec());

    // NUL within the binary sample is reported, not decoded
    std::fs::write(sandbox.root.join("nul.txt"), b"a\x00b\n").unwrap();
    let page = read::read_text_page(&root, &["nul.txt"], 1, 10, 4096, 1024).unwrap();
    assert!(page.has_nul);

    // an over-long line past start_line is LINE_TOO_LARGE, not truncation
    std::fs::write(sandbox.root.join("long.txt"), vec![b'x'; 300]).unwrap();
    let err = read::read_text_page(&root, &["long.txt"], 1, 10, 100, 1024).unwrap_err();
    assert!(matches!(
        err,
        NativeError::LineTooLarge {
            line: 1,
            max_bytes: 100
        }
    ));

    // start_line beyond EOF yields an empty page, no error
    let page = read::read_text_page(&root, &["basic.txt"], 9, 10, 4096, 1024).unwrap();
    assert!(page.lines.is_empty());
    assert!(!page.has_more);
    assert_eq!(page.end_line, 8);
}

#[test]
fn read_bounded_enforces_limits_and_integrity() {
    let sandbox = Sandbox::new("bounded");
    let root = open(&sandbox);
    std::fs::write(sandbox.root.join("blob.bin"), b"payload-42").unwrap();

    let out = read::read_bounded(&root, &["blob.bin"], 64).unwrap();
    assert_eq!(out.data, b"payload-42");
    assert_eq!(
        read::sha256_hex(&out.data),
        "7904c5cc6ec119a3d480852eabcac905e2324213d534e7c34c78c90316fcb9d8"
    );
    assert_eq!(out.metadata.revision(), revision_of(&root, &["blob.bin"]));

    // empty file is a valid exact read
    std::fs::write(sandbox.root.join("empty.bin"), b"").unwrap();
    let out = read::read_bounded(&root, &["empty.bin"], 10).unwrap();
    assert!(out.data.is_empty());

    let err = read::read_bounded(&root, &["blob.bin"], 8).unwrap_err();
    assert_eq!(err, NativeError::FileTooLarge);
}

#[test]
fn enumeration_reports_types_from_reopened_handles_including_reparse() {
    let sandbox = Sandbox::new("enum");
    std::fs::create_dir(sandbox.root.join("sub")).unwrap();
    std::fs::write(sandbox.root.join("a.txt"), b"x\n").unwrap();
    std::fs::write(sandbox.root.join("long.bin"), vec![b'q'; 70_000]).unwrap();
    if !make_junction(&sandbox.root.join("jlink"), &sandbox.root.join("sub")) {
        panic!("junction could not be created for enumeration coverage");
    }

    let root = open(&sandbox);
    let mut entries = serverfs_windows_native::enumerate::list_directory(&root).unwrap();
    entries.sort_by(|a, b| a.name.cmp(&b.name));
    let labels: Vec<(String, String, Option<u64>)> = entries
        .iter()
        .map(|e| {
            (
                e.name.clone(),
                e.metadata.type_label().to_string(),
                e.metadata.size,
            )
        })
        .collect();
    assert!(
        labels.contains(&("a.txt".to_string(), "file".to_string(), Some(2))),
        "{labels:?}"
    );
    assert!(
        labels.contains(&("sub".to_string(), "directory".to_string(), None)),
        "{labels:?}"
    );
    assert!(
        labels.contains(&("jlink".to_string(), "reparse_point".to_string(), None)),
        "{labels:?}"
    );
    // large file enumerated across multiple scan batches
    assert!(
        labels.contains(&("long.bin".to_string(), "file".to_string(), Some(70_000))),
        "{labels:?}"
    );

    // listing a subdirectory by relative chain
    serverfs_windows_native::enumerate::list_directory(
        &traversal::resolve(&root, &["sub"], OpenKind::Directory).unwrap(),
    )
    .unwrap();

    // listing through a junction remains impossible: resolve rejects it
    assert_eq!(
        traversal::resolve(&root, &["jlink"], OpenKind::Directory).err(),
        Some(NativeError::ReparsePoint)
    );
}

#[test]
#[ignore]
fn probe_scan_layer_visibility() {
    let sandbox = Sandbox::new("scanprobe");
    std::fs::write(
        sandbox.root.join("a.txt"),
        b"x
",
    )
    .unwrap();
    std::fs::write(
        sandbox.root.join("b.txt"),
        b"y
",
    )
    .unwrap();
    let root = open(&sandbox);
    let mut scan = serverfs_windows_native::ffi::DirectoryScan::new();
    let mut rounds = 0;
    while let Some(batch) = scan.next_batch(&root).unwrap() {
        println!("batch: {batch:?}");
        rounds += 1;
        if rounds > 20 {
            panic!("scan did not terminate");
        }
    }
    let entries = serverfs_windows_native::enumerate::list_directory(&root).unwrap();
    println!(
        "list_directory entries: {:?}",
        entries.iter().map(|e| &e.name).collect::<Vec<_>>()
    );
    for name in ["a.txt", "b.txt"] {
        let r = serverfs_windows_native::traversal::open_component_raw(&root, name);
        println!("reopen {name}: ok={}", r.is_ok());
        if let Ok(h) = r {
            println!("collect {name}: {:?}", metadata::collect(&h).is_ok());
        }
    }
}

#[test]
#[ignore]
fn probe_raw_batch_layout() {
    use serverfs_windows_native::ffi;
    let sandbox = Sandbox::new("rawlayout");
    std::fs::write(
        sandbox.root.join("a.txt"),
        b"x
",
    )
    .unwrap();
    let root = open(&sandbox);
    let mut buf = vec![0u8; 1024];
    let more = ffi::query_directory_batch(&root, &mut buf, true).unwrap();
    println!("more={more} bytes_valid~=");
    let mut off = 0usize;
    for e in 0..4 {
        let next = u32::from_le_bytes(buf[off..off + 4].try_into().unwrap());
        let eof = i64::from_le_bytes(buf[off + 40..off + 48].try_into().unwrap());
        let namelen4 = u32::from_le_bytes(buf[off + 52..off + 56].try_into().unwrap());
        let namelen5 = u32::from_le_bytes(buf[off + 56..off + 60].try_into().unwrap());
        let namelen8 = u32::from_le_bytes(buf[off + 58..off + 62].try_into().unwrap());
        println!("entry {e}: off={off} next={next} eof={eof} len@52={namelen4} len@56={namelen5} len@58={namelen8}");
        let hex: Vec<String> = buf[off..off + 128]
            .chunks(4)
            .map(|c| format!("{:02x?}", c))
            .collect();
        println!("  {hex:?}");
        if next == 0 {
            break;
        }
        off += next as usize;
    }
}

#[test]
#[ignore]
fn probe_which_material_moves_on_rename() {
    let sandbox = Sandbox::new("probemove");
    let file = sandbox.root.join("before.txt");
    std::fs::write(&file, b"content\n").unwrap();
    let root = open(&sandbox);
    let before_handle = traversal::resolve_for_report(&root, &["before.txt"]).unwrap();
    let before = metadata::collect(&before_handle).unwrap();
    let before_basic = serverfs_windows_native::ffi::basic_info(&before_handle).unwrap();
    std::fs::rename(&file, sandbox.root.join("after.txt")).unwrap();
    let after_handle = traversal::resolve_for_report(&root, &["after.txt"]).unwrap();
    let after = metadata::collect(&after_handle).unwrap();
    let after_basic = serverfs_windows_native::ffi::basic_info(&after_handle).unwrap();
    println!(
        "probe change_time (informational, NOT revision material) unchanged_on_rename={}",
        before_basic.ChangeTime == after_basic.ChangeTime
    );
    for (field, moved) in [
        ("id", before.id == after.id),
        ("attributes", before.attributes == after.attributes),
        ("size", before.size == after.size),
        ("links", before.link_count == after.link_count),
        (
            "last_write",
            before.last_write_100ns == after.last_write_100ns,
        ),
    ] {
        println!("probe {field} unchanged_on_rename={moved}");
    }
}
