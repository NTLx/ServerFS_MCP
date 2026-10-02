//! Bounded, handle-anchored file reads (§15.1).
//!
//! Both channels open the leaf as a short-lived synchronous file HANDLE
//! (only the root HANDLE is retained across operations), take a full
//! metadata snapshot from that handle before reading, and re-check the
//! revision from the same handle afterwards: content whose revision does
//! not describe it is never returned. The consistency transaction, byte
//! accounting and SHA-256 live here in the kernel; Python receives plain
//! results.
//!
//! The line loop deliberately reproduces `linux_backend`'s page
//! semantics in the same order: BOM strip on line 1, skipped lines before
//! `start_line` are not length-checked, the per-line size check runs
//! before the stop conditions, and a final line without a newline
//! terminates the scan exactly like iterating a Linux binary file object.

use sha2::{Digest, Sha256};

// Deterministic test seam (§29.4: fault injection is a test-build
// capability, never a production artifact one). The hook runs after the
// content pass and before the after-revision recheck, so a test can
// race an external write exactly where FILE_CHANGED_DURING_READ fires.
// It is thread-local: cargo tests run in parallel without interference.
#[cfg(test)]
thread_local! {
    static AFTER_PASS_HOOK: std::cell::RefCell<Option<Box<dyn Fn()>>> =
        std::cell::RefCell::new(None);
}

#[cfg(test)]
pub(crate) fn set_after_pass_hook(h: impl Fn() + 'static) {
    AFTER_PASS_HOOK.with(|c| *c.borrow_mut() = Some(Box::new(h)));
}

#[cfg(test)]
pub(crate) fn clear_after_pass_hook() {
    AFTER_PASS_HOOK.with(|c| *c.borrow_mut() = None);
}

#[cfg(not(test))]
#[inline]
fn fire_after_pass_hook() {}

#[cfg(test)]
#[inline]
fn fire_after_pass_hook() {
    AFTER_PASS_HOOK.with(|c| {
        if let Some(hook) = &*c.borrow() {
            hook();
        }
    });
}

use crate::error::NativeError;
use crate::metadata::{self, NativeMetadata};
use crate::{ffi, traversal, Handle};

const CHUNK: usize = 64 * 1024;

/// One paginated UTF-8 text read with revision stability — the Windows
/// producer for the backend-neutral `TextPage` contract.
#[derive(Debug)]
pub struct Page {
    pub revision: String,
    pub lines: Vec<Vec<u8>>,
    pub bytes_returned: u64,
    pub end_line: u64,
    pub has_more: bool,
    pub has_nul: bool,
    pub has_bom: bool,
}

pub fn read_text_page(
    root: &Handle,
    components: &[&str],
    start_line: u64,
    max_lines: u64,
    max_read_bytes: u64,
    binary_sample: u64,
) -> Result<Page, NativeError> {
    let file = traversal::resolve(root, components, ffi::OpenKind::File)?;
    let before = metadata::collect(&file)?;
    let sample = read_prefix(&file, binary_sample as usize)?;
    let has_nul = sample.contains(&0u8);
    let has_bom = sample.starts_with(b"\xEF\xBB\xBF");
    ffi::set_position(&file, 0)?;

    let mut lines: Vec<Vec<u8>> = Vec::new();
    let mut line_no: u64 = 0;
    let mut bytes_returned: u64 = 0;
    let mut end_line: u64 = start_line.saturating_sub(1);
    let mut has_more = false;
    let mut reader = LineReader::new(&file);
    while let Some(raw) = reader.next_line()? {
        if let LineOutcome::Stop = handle_line(
            raw,
            &mut line_no,
            start_line,
            max_lines,
            max_read_bytes,
            &mut lines,
            &mut bytes_returned,
            &mut end_line,
            &mut has_more,
            has_bom,
        )? {
            break;
        }
    }

    fire_after_pass_hook();
    if metadata::collect(&file)?.revision() != before.revision() {
        return Err(NativeError::ChangedDuringRead);
    }
    Ok(Page {
        revision: before.revision(),
        lines,
        bytes_returned,
        end_line,
        has_more,
        has_nul,
        has_bom,
    })
}

enum LineOutcome {
    Continue,
    Stop,
}

/// Streaming line iterator over a synchronous handle: yields
/// newline-terminated lines and one final unterminated line at EOF,
/// exactly like iterating a Linux binary file object.
struct LineReader<'a> {
    file: &'a Handle,
    rest: Vec<u8>,
    eof: bool,
}

impl<'a> LineReader<'a> {
    fn new(file: &'a Handle) -> LineReader<'a> {
        LineReader {
            file,
            rest: Vec::new(),
            eof: false,
        }
    }

    fn next_line(&mut self) -> Result<Option<Vec<u8>>, NativeError> {
        loop {
            if let Some(idx) = self.rest.iter().position(|b| *b == b'\n') {
                return Ok(Some(self.rest.drain(..=idx).collect()));
            }
            if self.eof {
                return if self.rest.is_empty() {
                    Ok(None)
                } else {
                    Ok(Some(std::mem::take(&mut self.rest)))
                };
            }
            let chunk = read_up_to(self.file, CHUNK)?;
            if chunk.is_empty() {
                self.eof = true;
            } else {
                self.rest.extend_from_slice(&chunk);
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_line(
    mut raw: Vec<u8>,
    line_no: &mut u64,
    start_line: u64,
    max_lines: u64,
    max_read_bytes: u64,
    lines: &mut Vec<Vec<u8>>,
    bytes_returned: &mut u64,
    end_line: &mut u64,
    has_more: &mut bool,
    has_bom: bool,
) -> Result<LineOutcome, NativeError> {
    *line_no += 1;
    if *line_no == 1 && has_bom {
        raw.drain(..3);
    }
    if *line_no < start_line {
        return Ok(LineOutcome::Continue);
    }
    if raw.len() as u64 > max_read_bytes {
        // LINE_TOO_LARGE aborts the whole call, exactly like Linux: an
        // over-long line past start_line is an error, not a truncation.
        return Err(NativeError::LineTooLarge {
            line: *line_no,
            max_bytes: max_read_bytes,
        });
    }
    if lines.len() as u64 >= max_lines {
        *has_more = true;
        return Ok(LineOutcome::Stop);
    }
    if *bytes_returned + raw.len() as u64 > max_read_bytes {
        *has_more = true;
        return Ok(LineOutcome::Stop);
    }
    *bytes_returned += raw.len() as u64;
    *end_line = *line_no;
    lines.push(raw);
    Ok(LineOutcome::Continue)
}

#[derive(Debug)]
pub struct BoundedRead {
    pub data: Vec<u8>,
    pub metadata: NativeMetadata,
}

/// Read one whole regular file, bounded, with before/after identity.
pub fn read_bounded(
    root: &Handle,
    components: &[&str],
    max_bytes: u64,
) -> Result<BoundedRead, NativeError> {
    let file = traversal::resolve(root, components, ffi::OpenKind::File)?;
    let before = metadata::collect(&file)?;
    let size = before.size.unwrap_or(0);
    if size > max_bytes {
        return Err(NativeError::FileTooLarge);
    }
    let mut data = Vec::with_capacity(size as usize);
    loop {
        let chunk = read_up_to(&file, CHUNK)?;
        if chunk.is_empty() {
            break;
        }
        if data.len() as u64 + chunk.len() as u64 > max_bytes {
            return Err(NativeError::FileTooLarge);
        }
        data.extend_from_slice(&chunk);
    }
    fire_after_pass_hook();
    if metadata::collect(&file)?.revision() != before.revision() {
        return Err(NativeError::ChangedDuringRead);
    }
    Ok(BoundedRead {
        data,
        metadata: before,
    })
}

pub fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

fn read_prefix(file: &Handle, want: usize) -> Result<Vec<u8>, NativeError> {
    let mut out = Vec::with_capacity(want);
    while out.len() < want {
        let chunk = read_up_to(file, (want - out.len()).min(CHUNK))?;
        if chunk.is_empty() {
            break;
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

fn read_up_to(file: &Handle, size: usize) -> Result<Vec<u8>, NativeError> {
    let mut buffer = vec![0u8; size];
    let read = ffi::read_chunk(file, &mut buffer)?;
    buffer.truncate(read);
    Ok(buffer)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::traversal;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static N: AtomicUsize = AtomicUsize::new(0);

    struct Dir(PathBuf);
    impl Dir {
        fn new() -> Dir {
            let p = std::env::temp_dir().join(format!(
                "serverfs_seam_{}_{:04}",
                std::process::id(),
                N.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&p).unwrap();
            Dir(p)
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn deterministic_file_changed_during_read_bounded() {
        let dir = Dir::new();
        let file = dir.0.join("a.bin");
        std::fs::write(&file, b"x".repeat(64)).unwrap();
        let root = traversal::open_root(dir.0.to_str().unwrap()).unwrap();
        set_after_pass_hook({
            let file = file.clone();
            move || {
                std::fs::write(&file, b"yy".repeat(64)).unwrap();
            }
        });
        let result = read_bounded(&root, &["a.bin"], 1 << 20);
        clear_after_pass_hook();
        assert!(matches!(result, Err(NativeError::ChangedDuringRead)));
    }

    #[test]
    fn deterministic_file_changed_during_read_page() {
        let dir = Dir::new();
        let file = dir.0.join("a.txt");
        std::fs::write(&file, b"one\ntwo\n").unwrap();
        let root = traversal::open_root(dir.0.to_str().unwrap()).unwrap();
        set_after_pass_hook({
            let file = file.clone();
            move || {
                std::fs::write(&file, b"one-MUTATED\ntwo-three\n").unwrap();
            }
        });
        let result = read_text_page(&root, &["a.txt"], 1, 10, 4096, 1024);
        clear_after_pass_hook();
        assert!(matches!(result, Err(NativeError::ChangedDuringRead)));
    }

    #[test]
    fn hook_absent_means_normal_read() {
        let dir = Dir::new();
        std::fs::write(dir.0.join("a.bin"), b"stable").unwrap();
        let root = traversal::open_root(dir.0.to_str().unwrap()).unwrap();
        let out = read_bounded(&root, &["a.bin"], 1 << 20).unwrap();
        assert_eq!(out.data, b"stable");
    }
}
