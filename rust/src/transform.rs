use std::collections::BTreeMap;

/// A single edit operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edit {
    pub start: usize,
    pub end: usize,
    pub replacement: String,
}

/// A collection of edits for a single file.
pub struct FileTransform {
    pub source: String,
    pub edits: Vec<Edit>,
}

impl FileTransform {
    pub fn new(source: String) -> Self {
        Self {
            source,
            edits: Vec::new(),
        }
    }

    /// Replace a range of bytes with new text.
    pub fn replace_range(&mut self, start: usize, end: usize, replacement: String) {
        self.edits.push(Edit {
            start,
            end,
            replacement,
        });
    }

    /// Insert text before a position.
    pub fn insert_before(&mut self, pos: usize, text: String) {
        self.replace_range(pos, pos, text);
    }

    /// Insert text after a position.
    pub fn insert_after(&mut self, pos: usize, text: String) {
        self.replace_range(pos, pos, text);
    }

    /// Remove a range of bytes.
    pub fn remove_range(&mut self, start: usize, end: usize) {
        self.replace_range(start, end, String::new());
    }

    /// Apply all edits to the source and return the result.
    /// Returns None if edits overlap.
    pub fn apply(&self) -> Option<String> {
        if self.edits.is_empty() {
            return Some(self.source.clone());
        }

        // Use a BTreeMap to sort edits by start position and check for overlaps.
        let mut sorted_edits = BTreeMap::new();
        for edit in &self.edits {
            if let Some(prev) = sorted_edits.insert(edit.start, edit) {
                // If two edits start at the same position, it's okay only if they are both insertions
                // or if they are identical (though that's redundant).
                // For simplicity, we'll disallow it for now unless we need more complex logic.
                if prev.end != edit.start || edit.end != edit.start {
                   return None;
                }
            }
        }

        let mut result = String::with_capacity(self.source.len());
        let mut last_pos = 0;

        for (&start, edit) in &sorted_edits {
            if start < last_pos {
                // Overlapping edits
                return None;
            }
            result.push_str(&self.source[last_pos..start]);
            result.push_str(&edit.replacement);
            last_pos = edit.end;
        }

        result.push_str(&self.source[last_pos..]);
        Some(result)
    }
}
