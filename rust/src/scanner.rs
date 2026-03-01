//! Fast parallel file scanner.

use std::path::{Path, PathBuf};

/// Directories to skip when scanning for Python files.
const SKIP_DIRS: &[&str] = &[
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".eggs",
    "dist",
    "build",
    ".nox",
    ".uv-cache",
    ".pixi",
    ".cargo",
    ".cargo-cache",
];

/// Collect all `.py` files under `root`, skipping non-project directories.
///
/// Follows symlinks to both directories and files.
pub fn collect_python_files(root: &Path) -> Vec<PathBuf> {
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
                if !SKIP_DIRS.contains(&name_str.as_ref()) {
                    stack.push(entry.path());
                }
            } else if is_file && name_str.ends_with(".py") {
                files.push(entry.path());
            }
        }
    }

    files
}
