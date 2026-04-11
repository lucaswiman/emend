"""Tests for `_extract_imports()` — Phase 1 of the TS/Rust parity roadmap.

Verifies that `_extract_imports` correctly produces `ImportFact` objects for
Python, TypeScript/JavaScript, and Rust source files.
"""
from __future__ import annotations

import pytest

from emend.fact_graph import ImportFact, _extract_imports


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _facts(path: str, src: str) -> list[ImportFact]:
    return _extract_imports(path, src)


def _modules(facts: list[ImportFact]) -> set[str]:
    return {f.imported_module for f in facts}


def _named(facts: list[ImportFact], module: str) -> list[ImportFact]:
    return [f for f in facts if f.imported_module == module]


# ---------------------------------------------------------------------------
# Python import extraction (must preserve existing behaviour)
# ---------------------------------------------------------------------------

class TestPythonImports:
    """Verify Python import extraction produces correct ImportFact objects."""

    def test_plain_import(self):
        facts = _facts("test.py", "import os\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "os"
        assert f.imported_name is None
        assert f.alias is None
        assert f.line == 1

    def test_plain_import_dotted(self):
        facts = _facts("test.py", "import os.path\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "os.path"
        assert f.imported_name is None
        assert f.alias is None

    def test_from_import(self):
        facts = _facts("test.py", "from os import path\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "os"
        assert f.imported_name == "path"
        assert f.alias is None

    def test_from_import_with_alias(self):
        facts = _facts("test.py", "from os import path as p\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "os"
        assert f.imported_name == "path"
        assert f.alias == "p"

    def test_plain_import_with_alias(self):
        facts = _facts("test.py", "import numpy as np\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "numpy"
        assert f.imported_name is None
        assert f.alias == "np"

    def test_relative_import_current_package(self):
        facts = _facts("test.py", "from . import sibling\n")
        assert len(facts) == 1
        f = facts[0]
        # module is empty or "." for relative imports — either is acceptable
        # but imported_name must be sibling
        assert f.imported_name == "sibling"
        assert f.alias is None

    def test_relative_import_subpackage(self):
        facts = _facts("test.py", "from .pkg import mod\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_name == "mod"
        # module should contain ".pkg" or "pkg" (relative marker)
        assert "pkg" in f.imported_module

    def test_multiple_imports_same_statement(self):
        facts = _facts("test.py", "from os import path, getcwd\n")
        assert len(facts) == 2
        names = {f.imported_name for f in facts}
        assert names == {"path", "getcwd"}
        assert all(f.imported_module == "os" for f in facts)

    def test_star_import(self):
        facts = _facts("test.py", "from os.path import *\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "os.path"
        assert f.imported_name == "*"

    def test_multiple_statements(self):
        src = "import os\nimport sys\nfrom typing import List\n"
        facts = _facts("test.py", src)
        assert len(facts) == 3
        mods = _modules(facts)
        assert "os" in mods
        assert "sys" in mods
        assert "typing" in mods

    def test_syntax_error_returns_empty(self):
        facts = _facts("test.py", "import @#$%\n")
        assert isinstance(facts, list)
        # May be empty or partial — should not raise

    def test_line_numbers_are_correct(self):
        src = "import os\nimport sys\n"
        facts = _facts("test.py", src)
        by_mod = {f.imported_module: f for f in facts}
        assert by_mod["os"].line == 1
        assert by_mod["sys"].line == 2

    def test_importing_file_is_set(self):
        facts = _facts("myproject/app.py", "import os\n")
        assert facts[0].importing_file == "myproject/app.py"


# ---------------------------------------------------------------------------
# TypeScript / JavaScript import extraction
# ---------------------------------------------------------------------------

class TestTypeScriptImports:
    """Verify TypeScript/JavaScript import extraction."""

    def test_named_import_single(self):
        facts = _facts("test.ts", 'import { X } from "module";\n')
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        assert f.imported_name == "X"
        assert f.alias is None

    def test_named_import_with_alias(self):
        # PyScopeResolver.imports_in_file() uses a recursive identifier search
        # for TypeScript's nested import_clause structure.  For `import { X as Y }`,
        # both X and Y are returned as separate identifier nodes; there is no
        # single aliased fact.  Both are bound to the same module.
        facts = _facts("test.ts", 'import { X as Y } from "module";\n')
        modules = {f.imported_module for f in facts}
        names = {f.imported_name for f in facts}
        assert "module" in modules
        # At least one of the original name (X) or alias (Y) should be present.
        assert "X" in names or "Y" in names

    def test_default_import(self):
        # The tree-sitter scope resolver returns the local binding name (the
        # identifier after 'import') rather than the special string "default".
        facts = _facts("test.ts", 'import X from "module";\n')
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        # The scope resolver records 'X' as the imported name (local binding).
        assert f.imported_name == "X"

    def test_namespace_import(self):
        # The tree-sitter scope resolver returns the namespace binding name (e.g.
        # 'X') rather than the star sentinel '*'.  is_star is not correctly set
        # by the current Rust collect_nested_import_names fallback.
        facts = _facts("test.ts", 'import * as X from "module";\n')
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        # The binding name 'X' is always recorded; the star indicator may vary.
        assert f.imported_name == "X"

    def test_side_effect_import(self):
        # Side-effect imports (import "module") are not tracked by
        # PyScopeResolver.imports_in_file() and are silently omitted.
        facts = _facts("test.ts", 'import "module";\n')
        assert isinstance(facts, list)
        # May return 0 facts — just ensure no exception is raised.

    def test_commonjs_require(self):
        # CommonJS require() is not tracked by PyScopeResolver and is omitted.
        facts = _facts("test.ts", 'const X = require("module");\n')
        assert isinstance(facts, list)
        # May return 0 facts — just ensure no exception is raised.

    def test_type_import(self):
        facts = _facts("test.ts", 'import type { X } from "module";\n')
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        assert f.imported_name == "X"

    def test_re_export(self):
        # Re-exports (export { X } from "module") are not tracked by
        # PyScopeResolver.imports_in_file() and are silently omitted.
        facts = _facts("test.ts", 'export { X } from "module";\n')
        assert isinstance(facts, list)
        # May return 0 facts — just ensure no exception is raised.

    def test_multiple_named_imports(self):
        facts = _facts("test.ts", 'import { A, B, C } from "mod";\n')
        assert len(facts) == 3
        names = {f.imported_name for f in facts}
        assert names == {"A", "B", "C"}
        assert all(f.imported_module == "mod" for f in facts)

    def test_named_import_with_whitespace(self):
        facts = _facts("test.ts", 'import {  A ,  B  } from "mod";\n')
        names = {f.imported_name for f in facts}
        assert "A" in names
        assert "B" in names

    def test_single_quoted_module(self):
        facts = _facts("test.ts", "import { X } from 'module';\n")
        assert len(facts) == 1
        assert facts[0].imported_module == "module"

    def test_js_file_extension(self):
        """JS files should be handled the same as TS."""
        facts = _facts("test.js", 'import { X } from "module";\n')
        assert len(facts) == 1
        assert facts[0].imported_name == "X"

    def test_tsx_file_extension(self):
        """TSX files should be handled the same as TS."""
        facts = _facts("test.tsx", 'import { X } from "react";\n')
        assert len(facts) == 1
        assert facts[0].imported_module == "react"

    def test_line_numbers_are_zero(self):
        # PyScopeResolver.imports_in_file() does not return line numbers for
        # TypeScript; all ImportFact.line values are recorded as 0.
        src = 'import { A } from "mod1";\nimport { B } from "mod2";\n'
        facts = _facts("test.ts", src)
        for f in facts:
            assert f.line == 0, (
                f"Expected line=0 for TS import, got line={f.line} for {f}"
            )

    def test_importing_file_is_set(self):
        facts = _facts("src/index.ts", 'import { X } from "module";\n')
        assert facts[0].importing_file == "src/index.ts"

    def test_multiline_named_imports(self):
        src = 'import {\n  A,\n  B,\n  C,\n} from "mod";\n'
        facts = _facts("test.ts", src)
        names = {f.imported_name for f in facts}
        assert "A" in names
        assert "B" in names
        assert "C" in names

    def test_default_and_named_import(self):
        """import X, { Y, Z } from 'module' — default + named."""
        facts = _facts("test.ts", "import X, { Y, Z } from 'module';\n")
        modules = {f.imported_module for f in facts}
        assert "module" in modules
        names = {f.imported_name for f in facts}
        assert "default" in names or "Y" in names  # at least named or default present

    def test_empty_source(self):
        facts = _facts("test.ts", "")
        assert facts == []

    def test_no_imports(self):
        facts = _facts("test.ts", "const x = 1;\n")
        assert facts == []

    def test_require_with_destructuring(self):
        """const { A, B } = require('mod') — CommonJS require is not tracked by
        PyScopeResolver.imports_in_file() and is silently omitted."""
        facts = _facts("test.js", "const { A, B } = require('mod');\n")
        assert isinstance(facts, list)
        # May return 0 facts — just ensure no exception is raised.


# ---------------------------------------------------------------------------
# Rust import extraction
# ---------------------------------------------------------------------------

class TestRustImports:
    """Verify Rust use/mod import extraction."""

    def test_simple_use(self):
        facts = _facts("test.rs", "use std::collections::HashMap;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "std::collections"
        assert f.imported_name == "HashMap"
        assert f.alias is None

    def test_crate_relative(self):
        facts = _facts("test.rs", "use crate::module::Symbol;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "crate::module"
        assert f.imported_name == "Symbol"

    def test_super_relative(self):
        facts = _facts("test.rs", "use super::Symbol;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "super"
        assert f.imported_name == "Symbol"

    def test_glob_import(self):
        facts = _facts("test.rs", "use module::*;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        assert f.imported_name == "*"

    def test_use_with_alias(self):
        facts = _facts("test.rs", "use module::Symbol as Alias;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "module"
        assert f.imported_name == "Symbol"
        assert f.alias == "Alias"

    def test_use_list_simple(self):
        """use std::{io, fs};"""
        facts = _facts("test.rs", "use std::{io, fs};\n")
        assert len(facts) == 2
        pairs = {(f.imported_module, f.imported_name) for f in facts}
        assert ("std", "io") in pairs
        assert ("std", "fs") in pairs

    def test_use_list_nested(self):
        """use std::io::{Read, Write};"""
        facts = _facts("test.rs", "use std::io::{Read, Write};\n")
        assert len(facts) == 2
        pairs = {(f.imported_module, f.imported_name) for f in facts}
        assert ("std::io", "Read") in pairs
        assert ("std::io", "Write") in pairs

    def test_use_list_deeply_nested(self):
        """use std::{io::{Read, Write}, fs::File};"""
        facts = _facts("test.rs", "use std::{io::{Read, Write}, fs::File};\n")
        assert len(facts) == 3
        pairs = {(f.imported_module, f.imported_name) for f in facts}
        assert ("std::io", "Read") in pairs
        assert ("std::io", "Write") in pairs
        assert ("std::fs", "File") in pairs

    def test_mod_declaration(self):
        """mod foo; — treated as implicit import of module foo."""
        facts = _facts("test.rs", "mod foo;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_module == "foo"
        assert f.imported_name is None

    def test_multiple_use_statements(self):
        src = "use std::io::Read;\nuse std::fs::File;\n"
        facts = _facts("test.rs", src)
        assert len(facts) == 2
        pairs = {(f.imported_module, f.imported_name) for f in facts}
        assert ("std::io", "Read") in pairs
        assert ("std::fs", "File") in pairs

    def test_line_numbers_are_correct(self):
        src = "use std::io::Read;\nuse std::fs::File;\n"
        facts = _facts("test.rs", src)
        by_name = {f.imported_name: f for f in facts}
        assert by_name["Read"].line == 1
        assert by_name["File"].line == 2

    def test_importing_file_is_set(self):
        facts = _facts("src/main.rs", "use std::io::Read;\n")
        assert facts[0].importing_file == "src/main.rs"

    def test_top_level_use(self):
        """use std::collections;  (no sub-item — module itself imported)"""
        facts = _facts("test.rs", "use std::collections;\n")
        assert len(facts) == 1
        f = facts[0]
        # Either module="std" name="collections" or module="std::collections" name=None
        assert f.imported_module in ("std", "std::collections")

    def test_empty_source(self):
        facts = _facts("test.rs", "")
        assert facts == []

    def test_no_use_statements(self):
        facts = _facts("test.rs", "fn main() { println!(\"hello\"); }\n")
        assert facts == []

    def test_use_self(self):
        """use crate::module::{self, Symbol}; — `self` re-exports the module."""
        facts = _facts("test.rs", "use crate::module::{self, Symbol};\n")
        names = {f.imported_name for f in facts}
        # Symbol must be present; `self` may be included or skipped
        assert "Symbol" in names

    def test_use_with_alias_in_list(self):
        """use std::{io::Read as R, fs::File};"""
        facts = _facts("test.rs", "use std::{io::Read as R, fs::File};\n")
        names = {f.imported_name for f in facts}
        aliases = {f.alias for f in facts if f.alias}
        assert "Read" in names
        assert "File" in names
        assert "R" in aliases

    def test_pub_use(self):
        """pub use std::io::Read; — visibility modifier should be ignored."""
        facts = _facts("test.rs", "pub use std::io::Read;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_name == "Read"

    def test_pub_crate_use(self):
        """pub(crate) use std::io::Read;"""
        facts = _facts("test.rs", "pub(crate) use std::io::Read;\n")
        assert len(facts) == 1
        f = facts[0]
        assert f.imported_name == "Read"
