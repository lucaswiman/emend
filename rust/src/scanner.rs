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

            let file_type = match entry.file_type() {
                Ok(ft) => ft,
                Err(_) => continue,
            };

            let name = entry.file_name();
            let name_str = name.to_string_lossy();

            if file_type.is_dir() {
                if !SKIP_DIRS.contains(&name_str.as_ref()) {
                    stack.push(entry.path());
                }
            } else if file_type.is_file() && name_str.ends_with(".py") {
                files.push(entry.path());
            }
        }
    }

    files
}
