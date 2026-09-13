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

        // Stable sorting retains the call order of insertions at one position.
        // Put insertions before a replacement with the same start so the
        // insertion is applied at the replacement's left boundary.
        let mut sorted_edits: Vec<_> = self.edits.iter().collect();
        sorted_edits.sort_by_key(|edit| (edit.start, edit.start != edit.end));

        if sorted_edits.iter().any(|edit| {
            edit.start > edit.end
                || edit.end > self.source.len()
                || !self.source.is_char_boundary(edit.start)
                || !self.source.is_char_boundary(edit.end)
        }) {
            return None;
        }

        let mut result = String::with_capacity(self.source.len());
        let mut last_pos = 0;

        for edit in sorted_edits {
            if edit.start < last_pos {
                // Overlapping edits
                return None;
            }
            result.push_str(&self.source[last_pos..edit.start]);
            result.push_str(&edit.replacement);
            last_pos = edit.end;
        }

        result.push_str(&self.source[last_pos..]);
        Some(result)
    }
}

#[cfg(test)]
mod tests {
    use super::FileTransform;

    #[test]
    fn retains_same_position_insertions_in_call_order() {
        let mut transform = FileTransform::new("x".into());
        transform.insert_before(0, "A".into());
        transform.insert_before(0, "B".into());
        assert_eq!(transform.apply(), Some("ABx".into()));
    }

    #[test]
    fn permits_insertions_at_replacement_boundaries() {
        let mut transform = FileTransform::new("abcd".into());
        transform.replace_range(1, 3, "X".into());
        transform.insert_before(1, "L".into());
        transform.insert_after(3, "R".into());
        assert_eq!(transform.apply(), Some("aLXRd".into()));
    }

    #[test]
    fn rejects_overlapping_and_invalid_ranges() {
        for edits in [vec![(1, 3), (2, 4)], vec![(3, 2)], vec![(0, 5)]] {
            let mut transform = FileTransform::new("abcd".into());
            for (start, end) in edits {
                transform.replace_range(start, end, "X".into());
            }
            assert_eq!(transform.apply(), None);
        }

        let mut transform = FileTransform::new("é".into());
        transform.replace_range(1, 2, "X".into());
        assert_eq!(transform.apply(), None);
    }
}
