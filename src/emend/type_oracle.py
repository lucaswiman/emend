"""Type inference adapter layer.

Provides an abstract TypeOracle interface for querying inferred types,
with a concrete implementation backed by Pyrefly. The adapter is designed
to be swappable — when ty (Astral) or another engine ships stable APIs,
a new adapter can be written against the same interface.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Type descriptor tree
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TypeDescriptor:
    """A structured representation of a Python type.

    This mirrors the proposal's TypeDescriptor enum but as a tagged union
    via the ``kind`` field.  The tree structure enables structural pattern
    matching against type constraints (e.g. ``List[$T]``).
    """
    kind: Literal["named", "parameterized", "union", "callable", "unknown"]
    name: str = ""
    params: tuple[TypeDescriptor, ...] = ()
    # For callable: params stores parameter types, return_type is the return
    return_type: TypeDescriptor | None = None

    # Convenience constructors ---
    @classmethod
    def named(cls, name: str) -> TypeDescriptor:
        return cls(kind="named", name=name)

    @classmethod
    def parameterized(cls, name: str, params: tuple[TypeDescriptor, ...]) -> TypeDescriptor:
        return cls(kind="parameterized", name=name, params=params)

    @classmethod
    def union(cls, members: tuple[TypeDescriptor, ...]) -> TypeDescriptor:
        return cls(kind="union", params=members)

    @classmethod
    def callable_(cls, param_types: tuple[TypeDescriptor, ...], ret: TypeDescriptor) -> TypeDescriptor:
        return cls(kind="callable", params=param_types, return_type=ret)

    @classmethod
    def unknown(cls) -> TypeDescriptor:
        return cls(kind="unknown")

    def display(self) -> str:
        """Human-readable type string."""
        if self.kind == "named":
            return self.name
        if self.kind == "parameterized":
            inner = ", ".join(p.display() for p in self.params)
            return f"{self.name}[{inner}]"
        if self.kind == "union":
            return " | ".join(p.display() for p in self.params)
        if self.kind == "callable":
            args = ", ".join(p.display() for p in self.params)
            ret = self.return_type.display() if self.return_type else "Unknown"
            return f"({args}) -> {ret}"
        return "Unknown"

    def matches(self, constraint: TypeDescriptor) -> bool:
        """Check if *self* satisfies *constraint* (structural match).

        Supports exact match and parameterized match with wildcards.
        Does NOT handle subtype matching — that requires supertype info
        from the type oracle.
        """
        if constraint.kind == "unknown":
            return True  # wildcard
        if self.kind == "unknown":
            return False
        # If self is a union, check if any member matches the constraint
        if self.kind == "union" and constraint.kind != "union":
            return any(m.matches(constraint) for m in self.params)
        if constraint.kind == "named":
            if self.kind == "named":
                return self.name == constraint.name
            if self.kind == "parameterized":
                return self.name == constraint.name  # List matches List[int]
            return False
        if constraint.kind == "parameterized":
            if self.kind == "parameterized" and self.name == constraint.name:
                if len(self.params) != len(constraint.params):
                    return False
                return all(a.matches(b) for a, b in zip(self.params, constraint.params))
            return False
        if constraint.kind == "union":
            if self.kind == "union":
                # At least one member of self matches at least one constraint member
                return any(
                    sm.matches(cm)
                    for sm in self.params
                    for cm in constraint.params
                )
            # Self must match at least one member of the constraint union
            return any(self.matches(m) for m in constraint.params)
        if constraint.kind == "callable":
            if self.kind != "callable":
                return False
            if len(self.params) != len(constraint.params):
                return False
            if not all(a.matches(b) for a, b in zip(self.params, constraint.params)):
                return False
            if constraint.return_type and self.return_type:
                return self.return_type.matches(constraint.return_type)
            return constraint.return_type is None
        return False


# ---------------------------------------------------------------------------
# Binding / type info for a single expression or symbol
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TypeBinding:
    """Type information for a single source location."""
    name: str
    line: int
    col_start: int
    col_end: int | None
    type_descriptor: TypeDescriptor
    raw_type: str  # Original type string from the engine
    binding_kind: str  # "definition", "reference", "import", etc.


@dataclass(slots=True)
class FileTypes:
    """All type bindings for a single file."""
    path: str
    bindings: list[TypeBinding] = field(default_factory=list)
    # Indexed by (line, col) for fast positional lookup
    _by_position: dict[tuple[int, int], TypeBinding] = field(default_factory=dict, repr=False)
    # Indexed by name for symbol lookup
    _by_name: dict[str, list[TypeBinding]] = field(default_factory=dict, repr=False)

    def build_index(self) -> None:
        """Build positional and name indexes from bindings."""
        self._by_position.clear()
        self._by_name.clear()
        for b in self.bindings:
            self._by_position[(b.line, b.col_start)] = b
            self._by_name.setdefault(b.name, []).append(b)

    def type_at(self, line: int, col: int) -> TypeBinding | None:
        return self._by_position.get((line, col))

    def types_for_name(self, name: str) -> list[TypeBinding]:
        return self._by_name.get(name, [])

    def definitions(self) -> list[TypeBinding]:
        return [b for b in self.bindings if b.binding_kind == "definition"]


# ---------------------------------------------------------------------------
# Abstract TypeOracle interface
# ---------------------------------------------------------------------------

class TypeOracle(ABC):
    """Abstract interface for querying inferred types.

    Implementations wrap a specific type checker backend (Pyrefly, ty, etc.).
    The interface is intentionally minimal — consumers should be able to swap
    backends by changing a single constructor call.
    """

    @abstractmethod
    def infer_file(self, path: Path, project_root: Path | None = None) -> FileTypes:
        """Return inferred types for all symbols/expressions in a file."""

    @abstractmethod
    def type_at(self, path: Path, line: int, col: int,
                project_root: Path | None = None) -> TypeBinding | None:
        """Return the inferred type at a specific source position."""

    @abstractmethod
    def clear_cache(self) -> None:
        """Clear any cached type information."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the backing type checker is installed and usable."""


# ---------------------------------------------------------------------------
# Pyrefly type string parser
# ---------------------------------------------------------------------------

# Tokenizer for type strings like "list[Connection]", "str | None",
# "(self: Self@Connection, host: str) -> None"

def _parse_type_string(raw: str) -> TypeDescriptor:
    """Parse a Pyrefly result type string into a TypeDescriptor tree.

    Handles: named types, parameterized types, unions, callable signatures.
    Falls back to TypeDescriptor.named(raw) for anything unparseable.
    """
    raw = raw.strip()
    if not raw or raw == "Unknown":
        return TypeDescriptor.unknown()

    # Handle union: "str | None"
    # But we need to be careful not to split inside brackets
    if " | " in raw and not raw.startswith("("):
        parts = _split_union(raw)
        if len(parts) > 1:
            return TypeDescriptor.union(tuple(_parse_type_string(p) for p in parts))

    # Handle callable: "(args...) -> ReturnType"
    if raw.startswith("(") and " -> " in raw:
        return _parse_callable(raw)

    # Handle Overload[...] — just return the first overload signature for now
    if raw.startswith("Overload["):
        return TypeDescriptor.named("Overload")

    # Handle parameterized: "list[Connection]", "dict[str, int]", "type[X]"
    bracket_pos = raw.find("[")
    if bracket_pos != -1 and raw.endswith("]"):
        name = raw[:bracket_pos].strip()
        inner = raw[bracket_pos + 1:-1]
        params = _split_params(inner)
        return TypeDescriptor.parameterized(
            name, tuple(_parse_type_string(p) for p in params)
        )

    # Plain named type
    # Strip Self@ prefix: "Self@Connection" -> "Connection"
    if raw.startswith("Self@"):
        return TypeDescriptor.named(raw[5:])

    return TypeDescriptor.named(raw)


def _split_union(raw: str) -> list[str]:
    """Split a union type string on ' | ' respecting bracket nesting."""
    parts = []
    depth = 0
    paren_depth = 0
    current: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "(":
            paren_depth += 1
            current.append(ch)
        elif ch == ")":
            paren_depth -= 1
            current.append(ch)
        elif ch == "|" and depth == 0 and paren_depth == 0:
            # Check for " | " pattern
            if i > 0 and raw[i-1] == " " and i + 1 < len(raw) and raw[i+1] == " ":
                part = "".join(current).rstrip()
                parts.append(part)
                current = []
                i += 2  # skip "| "
                continue
            else:
                current.append(ch)
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return parts


def _split_params(raw: str) -> list[str]:
    """Split comma-separated type parameters respecting bracket nesting."""
    parts = []
    depth = 0
    paren_depth = 0
    current: list[str] = []
    for ch in raw:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "(":
            paren_depth += 1
            current.append(ch)
        elif ch == ")":
            paren_depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0 and paren_depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def _parse_callable(raw: str) -> TypeDescriptor:
    """Parse a callable type like '(self: Self@Foo, x: int) -> str'."""
    # Find the matching close paren
    depth = 0
    close_paren = -1
    for i, ch in enumerate(raw):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_paren = i
                break
    if close_paren == -1:
        return TypeDescriptor.named(raw)

    params_str = raw[1:close_paren]
    rest = raw[close_paren + 1:].strip()
    if rest.startswith("-> "):
        ret_str = rest[3:].strip()
    elif rest.startswith("->"):
        ret_str = rest[2:].strip()
    else:
        ret_str = "None"

    # Parse parameter types (skip names, extract types after ':')
    param_types: list[TypeDescriptor] = []
    if params_str.strip():
        for param in _split_params(params_str):
            param = param.strip()
            if not param:
                continue
            # "self: Self@Foo" or "host: str" or just "str" (positional-only with /)
            if param in ("/", "*"):
                continue
            if ": " in param:
                type_part = param.split(": ", 1)[1]
                param_types.append(_parse_type_string(type_part))
            else:
                param_types.append(_parse_type_string(param))

    return TypeDescriptor.callable_(tuple(param_types), _parse_type_string(ret_str))


# ---------------------------------------------------------------------------
# Pyrefly debug-info JSON parser
# ---------------------------------------------------------------------------

# Location patterns: "3:7-17" (line:col_start-col_end) or "1:1"
_LOC_RE = re.compile(r"^(\d+):(\d+)(?:-(\d+))?$")

# Key patterns: Key::Definition(name loc), Key::BoundName(name loc)
_KEY_RE = re.compile(
    r"Key::(Definition|BoundName|Import|CompletedPartialType)"
    r"\((\w+)\s+([\d:.-]+)\)"
)


def _parse_location(loc_str: str) -> tuple[int, int, int | None]:
    """Parse '3:7-17' into (line, col_start, col_end)."""
    m = _LOC_RE.match(loc_str.strip())
    if not m:
        return (0, 0, None)
    line = int(m.group(1))
    col_start = int(m.group(2))
    col_end = int(m.group(3)) if m.group(3) else None
    return (line, col_start, col_end)


def _classify_binding_kind(key: str) -> str:
    """Classify a pyrefly binding key into a simpler kind."""
    if "Definition" in key:
        return "definition"
    if "BoundName" in key:
        return "reference"
    if "Import" in key:
        return "import"
    if "CompletedPartialType" in key:
        return "inferred"
    if "Annotation" in key:
        return "annotation"
    return "other"


def _parse_pyrefly_debug(debug_json: dict, file_path: str) -> FileTypes:
    """Parse pyrefly --debug-info JSON into a FileTypes object."""
    ft = FileTypes(path=file_path)

    modules = debug_json.get("modules", {})
    # Find the module that corresponds to our file.
    # Pyrefly uses dotted module names (e.g., "example" for "example.py",
    # "pkg.sub" for "pkg/sub.py").  Match against the file stem first,
    # then try converting dots to path separators for deeper matches.
    target_module = None
    file_stem = Path(file_path).stem
    file_path_normalized = file_path.replace("\\", "/")
    for mod_name in modules:
        if mod_name == file_stem:
            target_module = mod_name
            break
        # Convert "pkg.sub" to "pkg/sub" and check it appears as a
        # complete path segment (not a substring of another word).
        mod_as_path = mod_name.replace(".", "/")
        # Ensure we match a full segment: /pkg/sub.py not /xpkg/sub.py
        for suffix in (f"/{mod_as_path}.py", f"/{mod_as_path}/__init__.py"):
            if file_path_normalized.endswith(suffix):
                target_module = mod_name
                break
        if target_module:
            break

    if target_module is None:
        # Fall back to the first (or only) module
        if modules:
            target_module = next(iter(modules))
        else:
            return ft

    bindings_data = modules[target_module].get("bindings", [])
    # Track positions we've seen.  Maps (name, line, col) -> index in
    # ft.bindings so we can replace a non-definition with a definition
    # if the definition arrives later.
    seen_positions: dict[tuple[str, int, int], int] = {}

    for entry in bindings_data:
        key = entry.get("key", "")
        loc_str = entry.get("location", "")
        result = entry.get("result", "")

        if not result or result == "()":
            continue

        # Parse the key to extract name and kind
        m = _KEY_RE.match(key)
        if not m:
            continue

        kind_str = m.group(1)
        name = m.group(2)
        loc_part = m.group(3)
        line, col_start, col_end = _parse_location(loc_part)

        if line == 0:
            continue

        # Skip builtins (all imports at 1:1 are builtins)
        if kind_str == "Import" and loc_part == "1:1":
            continue

        binding_kind = _classify_binding_kind(key)

        # Deduplicate: prefer Definition over other kinds at same position.
        pos_key = (name, line, col_start)
        if pos_key in seen_positions:
            if binding_kind == "definition":
                # Replace the earlier non-definition entry
                ft.bindings[seen_positions[pos_key]] = None  # type: ignore[call-overload]
            else:
                continue

        type_desc = _parse_type_string(result)
        binding = TypeBinding(
            name=name,
            line=line,
            col_start=col_start,
            col_end=col_end,
            type_descriptor=type_desc,
            raw_type=result,
            binding_kind=binding_kind,
        )
        seen_positions[pos_key] = len(ft.bindings)
        ft.bindings.append(binding)

    # Remove None placeholders left by deduplication replacements
    ft.bindings = [b for b in ft.bindings if b is not None]
    ft.build_index()
    return ft


# ---------------------------------------------------------------------------
# Content-hash file cache
# ---------------------------------------------------------------------------

class _FileTypeCache:
    """Thread-safe cache of FileTypes keyed by file content hash.

    This avoids re-running pyrefly when a file hasn't changed.
    The cache is bounded by max_entries to prevent unbounded memory growth.
    Eviction is FIFO (first-inserted entries are evicted first).
    """

    def __init__(self, max_entries: int = 256):
        self._cache: dict[str, FileTypes] = {}  # content_hash -> FileTypes
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, content_hash: str) -> FileTypes | None:
        with self._lock:
            return self._cache.get(content_hash)

    def put(self, content_hash: str, ft: FileTypes) -> None:
        with self._lock:
            if len(self._cache) >= self._max_entries:
                # Evict first-inserted ~25% (FIFO)
                keys = list(self._cache.keys())[:self._max_entries // 4]
                for k in keys:
                    del self._cache[k]
            self._cache[content_hash] = ft

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


def _content_hash(path: Path) -> str:
    """Compute a fast content hash for a file."""
    content = path.read_bytes()
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Pyrefly adapter
# ---------------------------------------------------------------------------

class PyreflyAdapter(TypeOracle):
    """TypeOracle implementation backed by the Pyrefly CLI.

    Shells out to ``pyrefly check --debug-info`` and parses the JSON output
    to extract type bindings.  Results are cached per-file (keyed on content
    hash) to avoid re-running the type checker for unchanged files.

    This is the Phase 1 (CLI/subprocess) approach from the proposal.
    It can be replaced by a direct Rust crate integration in Phase 2.
    """

    def __init__(
        self,
        pyrefly_path: str | None = None,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
    ):
        self._pyrefly = pyrefly_path or shutil.which("pyrefly") or "pyrefly"
        self._cache = _FileTypeCache(max_entries=cache_size)
        self._extra_args = extra_args or []

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                [self._pyrefly, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def infer_file(self, path: Path, project_root: Path | None = None) -> FileTypes:
        path = path.resolve()
        if not path.exists():
            return FileTypes(path=str(path))

        # Check cache first
        content_hash = _content_hash(path)
        cached = self._cache.get(content_hash)
        if cached is not None:
            return cached

        # Run pyrefly
        debug_json = self._run_pyrefly(path, project_root)
        if debug_json is None:
            ft = FileTypes(path=str(path))
        else:
            ft = _parse_pyrefly_debug(debug_json, str(path))

        self._cache.put(content_hash, ft)
        return ft

    def type_at(self, path: Path, line: int, col: int,
                project_root: Path | None = None) -> TypeBinding | None:
        ft = self.infer_file(path, project_root)
        return ft.type_at(line, col)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _run_pyrefly(self, path: Path, project_root: Path | None) -> dict | None:
        """Run pyrefly check on a single file and return debug-info JSON."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            debug_path = tmp.name

        try:
            cmd = [
                self._pyrefly, "check",
                "--output-format", "json",
                "--debug-info", debug_path,
                "--summary=none",
                *self._extra_args,
                str(path),
            ]

            cwd = str(project_root) if project_root else str(path.parent)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=cwd,
            )

            # pyrefly may return non-zero for type errors — that's fine,
            # we still get debug-info
            if os.path.exists(debug_path) and os.path.getsize(debug_path) > 0:
                with open(debug_path) as f:
                    return json.load(f)
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return None
        finally:
            try:
                os.unlink(debug_path)
            except OSError:
                pass

    def infer_batch(self, paths: list[Path], project_root: Path | None = None) -> dict[str, FileTypes]:
        """Infer types for multiple files in a single pyrefly invocation.

        More efficient than calling infer_file() per file because pyrefly
        can share module resolution across files.

        Returns a dict keyed by resolved absolute path strings.
        """
        results: dict[str, FileTypes] = {}
        to_check: list[Path] = []

        # Resolve all paths up front for consistent dict keys.
        resolved = [p.resolve() for p in paths]

        for rp in resolved:
            if not rp.exists():
                results[str(rp)] = FileTypes(path=str(rp))
                continue
            content_hash = _content_hash(rp)
            cached = self._cache.get(content_hash)
            if cached is not None:
                results[str(rp)] = cached
            else:
                to_check.append(rp)

        if not to_check:
            return results

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            debug_path = tmp.name

        try:
            cmd = [
                self._pyrefly, "check",
                "--output-format", "json",
                "--debug-info", debug_path,
                "--summary=none",
                *self._extra_args,
                *[str(p) for p in to_check],
            ]

            cwd = str(project_root) if project_root else str(to_check[0].parent)

            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=cwd,
            )

            if os.path.exists(debug_path) and os.path.getsize(debug_path) > 0:
                with open(debug_path) as f:
                    debug_json = json.load(f)

                for path_obj in to_check:
                    ft = _parse_pyrefly_debug(debug_json, str(path_obj))
                    content_hash = _content_hash(path_obj)
                    self._cache.put(content_hash, ft)
                    results[str(path_obj)] = ft
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        finally:
            try:
                os.unlink(debug_path)
            except OSError:
                pass

        # Fill in empty results for files that weren't in the output
        for rp in to_check:
            key = str(rp)
            if key not in results:
                results[key] = FileTypes(path=key)

        return results


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_type_oracle(
    engine: str = "pyrefly",
    **kwargs,
) -> TypeOracle:
    """Create a TypeOracle instance for the specified engine.

    Args:
        engine: The type inference engine to use. Currently only "pyrefly"
                is supported.
        **kwargs: Additional keyword arguments passed to the adapter constructor.

    Returns:
        A TypeOracle instance.

    Raises:
        ValueError: If the engine is not recognized.
    """
    if engine == "pyrefly":
        return PyreflyAdapter(**kwargs)
    raise ValueError(
        f"Unknown type inference engine: {engine!r}. "
        f"Supported engines: pyrefly"
    )
