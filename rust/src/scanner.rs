//! Fast parallel file scanner.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

/// Non-dot directories to skip when scanning for Python files.
/// All directories starting with '.' are skipped automatically.
pub const SKIP_DIRS: &[&str] = &[
    "__pycache__",
    "venv",
    "node_modules",
    "dist",
    "build",
];

/// Collect all files under `root` with specific extensions, skipping non-project directories.
///
/// Follows symlinks to both directories and files, skipping directory cycles.
pub fn collect_files(root: &Path, extensions: &[&str]) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![(root.to_path_buf(), false)];
    let mut ancestors = HashSet::new();
    let dotted: Vec<String> = extensions.iter().map(|ext| format!(".{}", ext)).collect();

    while let Some((dir, exiting)) = stack.pop() {
        if exiting {
            ancestors.remove(&dir);
            continue;
        }
        let canonical = match dir.canonicalize() {
            Ok(path) => path,
            Err(_) => continue,
        };
        if !ancestors.insert(canonical.clone()) {
            continue;
        }
        stack.push((canonical, true));
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };

        for entry in entries {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };

            let name = entry.file_name();
            let name_str = name.to_string_lossy();

            // Check file type; follow symlinks only when needed.
            let ft = match entry.file_type() {
                Ok(ft) if ft.is_symlink() => match std::fs::metadata(entry.path()) {
                    Ok(meta) => meta.file_type(),
                    Err(_) => continue,
                },
                Ok(ft) => ft,
                Err(_) => continue,
            };

            if ft.is_dir() {
                // Skip all dot-directories (e.g. .venv, .poetry_cache, .git)
                // as well as explicitly listed non-dot directories.
                if !name_str.starts_with('.') && !SKIP_DIRS.contains(&name_str.as_ref()) {
                    stack.push((entry.path(), false));
                }
            } else if ft.is_file() && dotted.iter().any(|d| name_str.ends_with(d.as_str())) {
                files.push(entry.path());
            }
        }
    }

    files
}

pub fn collect_python_files(root: &Path) -> Vec<PathBuf> {
    collect_files(root, &["py", "pyi"])
}
