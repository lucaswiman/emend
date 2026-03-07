//! Fast parallel file scanner.

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
/// Follows symlinks to both directories and files.
pub fn collect_files(root: &Path, extensions: &[&str]) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];

    while let Some(dir) = stack.pop() {
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
                Ok(ft) => ft,
                Err(_) => continue,
            };
            let is_dir;
            let is_file;
            if ft.is_symlink() {
                // Follow symlink to determine actual type
                if let Ok(meta) = std::fs::metadata(entry.path()) {
                    is_dir = meta.is_dir();
                    is_file = meta.is_file();
                } else {
                    continue;
                }
            } else {
                is_dir = ft.is_dir();
                is_file = ft.is_file();
            }

            if is_dir {
                // Skip all dot-directories (e.g. .venv, .poetry_cache, .git)
                // as well as explicitly listed non-dot directories.
                if !name_str.starts_with('.') && !SKIP_DIRS.contains(&name_str.as_ref()) {
                    stack.push(entry.path());
                }
            } else if is_file {
                if extensions.iter().any(|ext| name_str.ends_with(&format!(".{}", ext))) {
                    files.push(entry.path());
                }
            }
        }
    }

    files
}

pub fn collect_python_files(root: &Path) -> Vec<PathBuf> {
    collect_files(root, &["py", "pyi"])
}
