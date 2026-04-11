"""Type inference adapter layer.

Provides an abstract TypeOracle interface for querying inferred types,
with concrete implementations backed by Pyrefly, Pyright, and ty.
The adapter is designed to be swappable — consumers select an engine
via ``create_type_oracle()`` or let autodetection pick one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Any


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LSP Client implementation
# ---------------------------------------------------------------------------

class LSPClient:
    """A minimal LSP client for querying type information."""

    def __init__(self, command: list[str], root_path: Path):
        self.command = command
        self.root_path = root_path
        self.process: subprocess.Popen | None = None
        self._id_counter = 0
        self._responses: dict[int, Any] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._read_thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start the LSP server process and initialize it."""
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError:
            return False

        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

        # Initialize
        init_res = self.send_request("initialize", {
            "processId": os.getpid(),
            "rootPath": str(self.root_path),
            "rootUri": self.root_path.as_uri(),
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]}
                }
            },
        })
        if init_res is None:
            return False

        self.send_notification("initialized", {})
        return True

    def stop(self):
        """Stop the LSP server gracefully via the LSP protocol."""
        if not self.process:
            return
        proc = self.process
        # Send LSP shutdown request followed by exit notification
        try:
            self.send_request("shutdown", {}, timeout=5)
            self.send_notification("exit", {})
        except OSError:
            pass
        self.process = None
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    def _read_loop(self):
        """Read responses from the LSP server."""
        proc = self.process
        if not proc or not proc.stdout:
            return

        try:
            while proc.poll() is None:
                line = proc.stdout.readline()
                if not line:
                    break
                if line.startswith(b"Content-Length: "):
                    length = int(line[16:].strip())
                    # Read until \r\n\r\n
                    while line.strip():
                        line = proc.stdout.readline()

                    content = proc.stdout.read(length)
                    if not content:
                        break

                    try:
                        message = json.loads(content.decode("utf-8"))
                        if "id" in message:
                            with self._lock:
                                self._responses[message["id"]] = message
                                self._condition.notify_all()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except (OSError, ValueError):
            pass

    def send_request(self, method: str, params: dict, timeout: float = 10.0) -> Any:
        """Send an LSP request and wait for the response."""
        if not self.process or not self.process.stdin:
            return None

        with self._lock:
            msg_id = self._id_counter
            self._id_counter += 1

        message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        
        try:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()
        except OSError:
            return None

        # Wait for response
        start_time = time.time()
        with self._condition:
            while msg_id not in self._responses:
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    return None
                if not self._condition.wait(remaining):
                    return None
            return self._responses.pop(msg_id)

    def send_notification(self, method: str, params: dict):
        """Send an LSP notification."""
        if not self.process or not self.process.stdin:
            return

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        
        try:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()
        except OSError:
            pass

    def did_open(self, path: Path, text: str, language_id: str = "python"):
        """Send textDocument/didOpen."""
        self.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": path.as_uri(),
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        })

    def hover(self, path: Path, line: int, col: int) -> str | None:
        """Send textDocument/hover and return the type string."""
        res = self.send_request("textDocument/hover", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line - 1, "character": col - 1},
        })
        if not res or "result" not in res or res["result"] is None:
            return None
        
        contents = res["result"].get("contents", "")
        if isinstance(contents, dict):
            return contents.get("value")
        if isinstance(contents, list):
            # Prefer the first dict item (MarkupContent), then fall back to strings
            for item in contents:
                if isinstance(item, dict):
                    return item.get("value")
            # No dict items found — return the first string item
            for item in contents:
                if isinstance(item, str):
                    return item
        return contents


# ---------------------------------------------------------------------------
# AST traversal for symbol collection
# ---------------------------------------------------------------------------

def _collect_symbols(source: str) -> list[tuple[str, int, int, int]]:
    """Collect all identifiers and their positions in the source.

    Uses the Rust tree-sitter extension for fast parsing.
    Returns (name, line, start_col_1indexed, end_col_1indexed) tuples.
    """
    from emend import emend_core
    return emend_core.collect_identifier_positions(source)


# ---------------------------------------------------------------------------
# Type name matching
# ---------------------------------------------------------------------------

def type_name_matches(constraint_name: str, type_name: str) -> bool:
    """Match short or fully-qualified type name against a resolved type string.

    e.g. ``'Redis'`` matches ``'redis.client.Redis'`` (last component).
    """
    if type_name == constraint_name:
        return True
    if "." not in constraint_name and "." in type_name:
        return type_name.rsplit(".", 1)[-1] == constraint_name
    return False


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
            # Special-case Rust reference: &[str] -> &str
            if self.name == "&" and self.params:
                return f"&{self.params[0].display()}"
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
                return type_name_matches(constraint.name, self.name)
            if self.kind == "parameterized":
                return type_name_matches(constraint.name, self.name)
            return False
        if constraint.kind == "parameterized":
            if self.kind == "parameterized" and type_name_matches(constraint.name, self.name):
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
        logger.debug(
            "Type index built for %s: %d bindings, %d positions, %d unique names",
            self.path, len(self.bindings), len(self._by_position), len(self._by_name),
        )

    def type_at(self, line: int, col: int) -> TypeBinding | None:
        return self._by_position.get((line, col))

    def types_for_name(self, name: str) -> list[TypeBinding]:
        return self._by_name.get(name, [])

    def definitions(self) -> list[TypeBinding]:
        return [b for b in self.bindings if b.binding_kind == "definition"]


# ---------------------------------------------------------------------------
# Abstract TypeOracle interface
# ---------------------------------------------------------------------------


class TypeEngineUnavailableError(RuntimeError):
    """Raised when the requested type inference engine is not installed."""


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

    def infer_batch(
        self, paths: list[Path], project_root: Path | None = None
    ) -> dict[str, FileTypes]:
        """Infer types for multiple files.

        Default implementation calls :meth:`infer_file` for each path.
        Subclasses may override for more efficient bulk processing (e.g.
        :class:`PyreflyAdapter` runs a single ``pyrefly check`` invocation).

        Returns a dict mapping resolved absolute path strings to
        :class:`FileTypes`.
        """
        results: dict[str, FileTypes] = {}
        for path in paths:
            resolved = path.resolve()
            if resolved.exists():
                try:
                    results[str(resolved)] = self.infer_file(resolved, project_root)
                except Exception:
                    logger.debug("infer_file failed for %s", resolved, exc_info=True)
                    results[str(resolved)] = FileTypes(path=str(resolved))
        return results


# ---------------------------------------------------------------------------
# Pyrefly type string parser
# ---------------------------------------------------------------------------

# Tokenizer for type strings like "list[Connection]", "str | None",
# "(self: Self@Connection, host: str) -> None"

def parse_type_string(raw: str) -> TypeDescriptor:
    """Parse a type checker result type string into a TypeDescriptor tree.

    Handles output from Pyrefly, Pyright, ty, TypeScript (tsc), and
    rust-analyzer: named types, parameterized types (both ``[]`` and ``<>``
    syntax), union types, callable signatures (``->`` and ``=>``), Rust
    reference types (``&``), and TypeScript array shorthand (``T[]``).

    Falls back to ``TypeDescriptor.named(raw)`` for anything unparseable.
    """
    raw = raw.strip()
    if not raw or raw == "Unknown":
        return TypeDescriptor.unknown()

    # Rust reference types: &str, &'a str, &mut T
    if raw.startswith("&"):
        inner = raw[1:].lstrip()
        # Strip lifetime: &'a str -> str
        if inner.startswith("'"):
            space_idx = inner.find(" ")
            if space_idx != -1:
                inner = inner[space_idx + 1:]
        # Strip mut: &mut T -> T
        if inner.startswith("mut "):
            inner = inner[4:]
        return TypeDescriptor.parameterized("&", (parse_type_string(inner),))

    # TypeScript array shorthand: string[] -> Array[string]
    # Don't match callable returns like (x: T) => U[] — skip if starts with (
    if raw.endswith("[]") and not raw.startswith("("):
        inner = raw[:-2]
        if inner:
            return TypeDescriptor.parameterized("Array", (parse_type_string(inner),))

    # Handle union: "str | None", "string | null"
    # But we need to be careful not to split inside brackets
    if " | " in raw and not raw.startswith("("):
        parts = _split_union(raw)
        if len(parts) > 1:
            return TypeDescriptor.union(tuple(parse_type_string(p) for p in parts))

    # Handle callable: "(args...) -> ReturnType" (Python/Rust)
    if raw.startswith("(") and " -> " in raw:
        return _parse_callable(raw)

    # Handle TypeScript arrow function: "(args...) => ReturnType"
    if raw.startswith("(") and " => " in raw:
        return _parse_callable_arrow(raw)

    # Handle Overload[...] — just return the first overload signature for now
    if raw.startswith("Overload["):
        return TypeDescriptor.named("Overload")

    # Handle parameterized with square brackets: "list[Connection]", "dict[str, int]"
    bracket_pos = raw.find("[")
    if bracket_pos != -1 and raw.endswith("]"):
        name = raw[:bracket_pos].strip()
        inner = raw[bracket_pos + 1:-1]
        params = _split_params(inner)
        return TypeDescriptor.parameterized(
            name, tuple(parse_type_string(p) for p in params)
        )

    # Handle parameterized with angle brackets: "Array<string>", "Vec<T>"
    angle_pos = raw.find("<")
    if angle_pos != -1 and raw.endswith(">"):
        name = raw[:angle_pos].strip()
        inner = raw[angle_pos + 1:-1]
        params = _split_params(inner)
        return TypeDescriptor.parameterized(
            name, tuple(parse_type_string(p) for p in params)
        )

    # Plain named type
    # Strip Self@ prefix: "Self@Connection" -> "Connection"
    if raw.startswith("Self@"):
        return TypeDescriptor.named(raw[5:])

    return TypeDescriptor.named(raw)


def _split_balanced(raw: str, delimiter: str) -> list[str]:
    """Split *raw* on *delimiter* respecting bracket/paren/angle/brace nesting.

    Handles ``[...]``, ``(...)``, ``<...>`` (TS/Rust generics), and
    ``{...}`` (TS object types) nesting.  The ``>`` character only
    decrements angle depth when a matching ``<`` was seen, so ``=>``
    arrow tokens are not misinterpreted.
    """
    parts: list[str] = []
    depth = 0
    paren_depth = 0
    angle_depth = 0
    brace_depth = 0
    current: list[str] = []
    i = 0
    dlen = len(delimiter)
    while i < len(raw):
        ch = raw[i]
        if ch == "[":
            depth += 1
            current.append(ch)
            i += 1
        elif ch == "]":
            depth -= 1
            current.append(ch)
            i += 1
        elif ch == "(":
            paren_depth += 1
            current.append(ch)
            i += 1
        elif ch == ")":
            paren_depth -= 1
            current.append(ch)
            i += 1
        elif ch == "<":
            angle_depth += 1
            current.append(ch)
            i += 1
        elif ch == ">" and angle_depth > 0:
            angle_depth -= 1
            current.append(ch)
            i += 1
        elif ch == "{":
            brace_depth += 1
            current.append(ch)
            i += 1
        elif ch == "}":
            brace_depth -= 1
            current.append(ch)
            i += 1
        elif (depth == 0 and paren_depth == 0 and angle_depth == 0
              and brace_depth == 0 and raw[i:i + dlen] == delimiter):
            parts.append("".join(current).strip())
            current = []
            i += dlen
        else:
            current.append(ch)
            i += 1
    if current:
        parts.append("".join(current).strip())
    return parts


def _split_union(raw: str) -> list[str]:
    """Split a union type string on ' | ' respecting bracket nesting."""
    return _split_balanced(raw, " | ")


def _split_params(raw: str) -> list[str]:
    """Split comma-separated type parameters respecting bracket nesting."""
    return _split_balanced(raw, ",")


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
                param_types.append(parse_type_string(type_part))
            else:
                param_types.append(parse_type_string(param))

    return TypeDescriptor.callable_(tuple(param_types), parse_type_string(ret_str))


def _parse_callable_arrow(raw: str) -> TypeDescriptor:
    """Parse a TypeScript arrow function type like '(a: string, b: number) => string'."""
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
    if rest.startswith("=> "):
        ret_str = rest[3:].strip()
    elif rest.startswith("=>"):
        ret_str = rest[2:].strip()
    else:
        ret_str = "void"

    param_types: list[TypeDescriptor] = []
    if params_str.strip():
        for param in _split_params(params_str):
            param = param.strip()
            if not param:
                continue
            if ": " in param:
                type_part = param.split(": ", 1)[1]
                param_types.append(parse_type_string(type_part))
            else:
                param_types.append(parse_type_string(param))

    return TypeDescriptor.callable_(tuple(param_types), parse_type_string(ret_str))


def _parse_rust_fn_signature(sig: str) -> str | None:
    """Extract a callable type string from a Rust function signature.

    Input:  ``"fn add(a: i32, b: i32) -> i32"``
    Output: ``"(i32, i32) -> i32"``
    """
    paren_start = sig.find("(")
    paren_end = sig.rfind(")")
    if paren_start == -1 or paren_end == -1:
        return None

    params_str = sig[paren_start + 1:paren_end]
    ret = sig[paren_end + 1:].strip()
    if ret.startswith("->"):
        ret = ret[2:].strip()
    else:
        ret = "()"

    param_types: list[str] = []
    for param in _split_balanced(params_str, ","):
        param = param.strip()
        if not param or param in ("self", "&self", "&mut self"):
            continue
        if ": " in param:
            param_types.append(param.split(": ", 1)[1].strip())

    types_str = ", ".join(param_types)
    return f"({types_str}) -> {ret}"


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
        # Prefer __unknown__ — pyrefly uses this as the module name for
        # standalone files (outside a proper Python project).  It always
        # refers to the file being analysed, so it is the correct target.
        if "__unknown__" in modules:
            target_module = "__unknown__"
        elif modules:
            # Fall back to the first (or only) module
            target_module = next(iter(modules))
        else:
            return ft

    bindings_data = modules[target_module].get("bindings", [])
    # Build the binding list, preferring Definition over other kinds at the
    # same (name, line, col) position.  Maps pos_key -> index in `bindings`.
    seen_positions: dict[tuple[str, int, int], int] = {}
    bindings: list[TypeBinding] = []

    for entry in bindings_data:
        key = entry.get("key", "")
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

        pos_key = (name, line, col_start)
        if pos_key in seen_positions:
            if binding_kind == "definition":
                # Upgrade the earlier non-definition entry in-place
                type_desc = parse_type_string(result)
                bindings[seen_positions[pos_key]] = TypeBinding(
                    name=name,
                    line=line,
                    col_start=col_start,
                    col_end=col_end,
                    type_descriptor=type_desc,
                    raw_type=result,
                    binding_kind=binding_kind,
                )
            continue

        type_desc = parse_type_string(result)
        seen_positions[pos_key] = len(bindings)
        bindings.append(TypeBinding(
            name=name,
            line=line,
            col_start=col_start,
            col_end=col_end,
            type_descriptor=type_desc,
            raw_type=result,
            binding_kind=binding_kind,
        ))

    ft.bindings = bindings
    ft.build_index()
    return ft


# ---------------------------------------------------------------------------
# Content-hash file cache
# ---------------------------------------------------------------------------

class _FileTypeCache:
    """Thread-safe two-tier cache of FileTypes keyed by file content hash.

    Tier 1: In-memory dict (fast, lost on exit).
    Tier 2: On-disk SQLite table (persists across invocations).

    This avoids re-running type checkers when a file hasn't changed.
    The in-memory cache is bounded by *max_entries*; eviction is FIFO.
    """

    def __init__(self, max_entries: int = 256, db_path: str | None = None):
        self._cache: dict[str, FileTypes] = {}  # content_hash -> FileTypes
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._db: _TypeOracleDiskCache | None = None
        if db_path is not None:
            self._db = _TypeOracleDiskCache(db_path)

    def get(self, content_hash: str) -> FileTypes | None:
        # Tier 1: memory
        with self._lock:
            cached = self._cache.get(content_hash)
        if cached is not None:
            return cached
        # Tier 2: disk
        if self._db is not None:
            ft = self._db.get(content_hash)
            if ft is not None:
                with self._lock:
                    self._cache[content_hash] = ft
                return ft
        return None

    def put(self, content_hash: str, ft: FileTypes) -> None:
        with self._lock:
            if len(self._cache) >= self._max_entries:
                # Evict first-inserted ~25% (FIFO)
                keys = list(self._cache.keys())[:self._max_entries // 4]
                for k in keys:
                    del self._cache[k]
            self._cache[content_hash] = ft
        # Persist to disk
        if self._db is not None:
            self._db.put(content_hash, ft)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
        if self._db is not None:
            self._db.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


class _TypeOracleDiskCache:
    """SQLite-backed persistent cache for TypeOracle results."""

    def __init__(self, db_path: str):
        import sqlite3
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS type_cache "
                "(hash TEXT PRIMARY KEY, data BLOB)"
            )
            self._conn.commit()
            logger.debug("type oracle disk cache opened at %s", db_path)
        except Exception as exc:
            logger.debug("type oracle disk cache unavailable: %s", exc)
            self._conn = None

    def get(self, content_hash: str) -> FileTypes | None:
        if self._conn is None:
            return None
        try:
            import pickle
            import zlib
            row = self._conn.execute(
                "SELECT data FROM type_cache WHERE hash = ?",
                (content_hash,),
            ).fetchone()
            if row is not None:
                ft = pickle.loads(zlib.decompress(row[0]))
                ft.build_index()
                return ft
        except Exception:
            pass
        return None

    def put(self, content_hash: str, ft: FileTypes) -> None:
        if self._conn is None:
            return
        try:
            import pickle
            import zlib
            data = zlib.compress(
                pickle.dumps(ft, protocol=pickle.HIGHEST_PROTOCOL), level=1
            )
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO type_cache VALUES (?, ?)",
                    (content_hash, data),
                )
                self._conn.commit()
        except Exception:
            pass

    def clear(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM type_cache")
                self._conn.commit()
        except Exception:
            pass


def _content_hash(path: Path) -> str:
    """Compute a fast content hash for a file."""
    content = path.read_bytes()
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


def _type_cache_db_path(project_root: Path | None = None) -> str | None:
    """Return the path to the type-oracle disk cache (parse.db), or None.

    Type inference results are stored in the same SQLite database as the parse
    and QN-index caches (``.emend/cache/parse.db``) in a ``type_cache`` table,
    so a single ``emend index`` run can populate all caches together.

    In a git worktree, the cache is shared with the main repo.
    """
    try:
        if project_root is None:
            from emend.transform import _find_project_root
            root = Path(_find_project_root("."))
        else:
            root = project_root
        from emend.transform import _cache_db_dir, _ensure_cache_ignore_files
        cache_dir = _cache_db_dir(root)
        cache_dir.mkdir(parents=True, exist_ok=True)
        _ensure_cache_ignore_files(str(root))
        return str(cache_dir / "parse.db")
    except Exception:
        return None


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
        db_path: str | None = None,
    ):
        self._pyrefly = pyrefly_path or shutil.which("pyrefly") or "pyrefly"
        self._cache = _FileTypeCache(max_entries=cache_size, db_path=db_path)
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
        logger.info("Building type index for %s via pyrefly", path)
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

            subprocess.run(
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
        # Pre-computed hashes for files that need checking, to avoid
        # recomputing after pyrefly returns.
        hashes: dict[str, str] = {}

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
                hashes[str(rp)] = content_hash

        if not to_check:
            return results

        logger.info(
            "Building type indexes for %d files via pyrefly (batch)",
            len(to_check),
        )
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
                    try:
                        ft = _parse_pyrefly_debug(debug_json, str(path_obj))
                    except Exception:
                        logger.debug("pyrefly parse failed for %s", path_obj, exc_info=True)
                        ft = FileTypes(path=str(path_obj))
                    content_hash = hashes[str(path_obj)]
                    self._cache.put(content_hash, ft)
                    results[str(path_obj)] = ft
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        finally:
            try:
                os.unlink(debug_path)
            except OSError:
                pass

        # Fill in empty results for files that weren't in the output and
        # cache them so subsequent runs don't re-invoke pyrefly for them.
        for rp in to_check:
            key = str(rp)
            if key not in results:
                ft = FileTypes(path=key)
                self._cache.put(hashes[key], ft)
                results[key] = ft

        return results


# ---------------------------------------------------------------------------
# Shared LSP-based TypeOracle base class
# ---------------------------------------------------------------------------

class _LSPTypeOracle(TypeOracle):
    """Base class for LSP-backed TypeOracle implementations.

    Subclasses provide ``_tool_name``, ``_lsp_command()``, and
    ``_parse_hover_type()``.  Everything else — LSP lifecycle, caching,
    and symbol iteration — is shared here.
    """

    _tool_name: str = ""  # For logging and error messages
    _language_id: str = "python"  # LSP languageId for textDocument/didOpen

    def __init__(
        self,
        tool_path: str,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
        db_path: str | None = None,
    ):
        self._tool = tool_path
        self._cache = _FileTypeCache(max_entries=cache_size, db_path=db_path)
        self._extra_args = extra_args or []
        self._lsp: LSPClient | None = None
        self._lsp_lock = threading.Lock()

    def is_available(self) -> bool:
        return shutil.which(self._tool) is not None

    def _lsp_command(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def _get_lsp(self, project_root: Path) -> LSPClient | None:
        with self._lsp_lock:
            if self._lsp is None:
                logger.info("Starting %s LSP server…", self._tool_name)
                self._lsp = LSPClient(self._lsp_command(), project_root)
                if not self._lsp.start():
                    self._lsp = None
            return self._lsp

    def infer_file(self, path: Path, project_root: Path | None = None) -> FileTypes:
        path = path.resolve()
        if not path.exists():
            return FileTypes(path=str(path))

        content_hash = _content_hash(path)
        cached = self._cache.get(content_hash)
        if cached is not None:
            return cached

        root = project_root or path.parent
        lsp = self._get_lsp(root)
        if not lsp:
            ft = FileTypes(path=str(path))
            self._cache.put(content_hash, ft)
            return ft

        try:
            logger.info("Building type index for %s via %s", path, self._tool_name)
            source = path.read_text(encoding="utf-8")
            lsp.did_open(path, source, language_id=self._language_id)

            symbols = _collect_symbols(source)
            ft = FileTypes(path=str(path))

            for name, line, col_start, col_end in symbols:
                hover_text = lsp.hover(path, line, col_start)
                if not hover_text:
                    continue

                raw_type = self._parse_hover_type(hover_text)
                if not raw_type:
                    continue

                type_desc = parse_type_string(raw_type)
                binding = TypeBinding(
                    name=name,
                    line=line,
                    col_start=col_start,
                    col_end=col_end,
                    type_descriptor=type_desc,
                    raw_type=raw_type,
                    binding_kind="inferred",
                )
                ft.bindings.append(binding)

            ft.build_index()
        except Exception:
            logger.debug("%s infer_file failed for %s", self._tool_name, path, exc_info=True)
            ft = FileTypes(path=str(path))

        self._cache.put(content_hash, ft)
        return ft

    def _parse_hover_type(self, hover_text: str) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def type_at(self, path: Path, line: int, col: int,
                project_root: Path | None = None) -> TypeBinding | None:
        ft = self.infer_file(path, project_root)
        return ft.type_at(line, col)

    def clear_cache(self) -> None:
        self._cache.clear()
        with self._lsp_lock:
            if self._lsp:
                self._lsp.stop()
                self._lsp = None

    def __del__(self):
        with self._lsp_lock:
            if self._lsp:
                self._lsp.stop()
                self._lsp = None


# ---------------------------------------------------------------------------
# Pyright adapter
# ---------------------------------------------------------------------------

class PyrightAdapter(_LSPTypeOracle):
    """TypeOracle implementation backed by the Pyright LSP.

    Starts a pyright-langserver instance and queries individual symbols
    via textDocument/hover to build a comprehensive type index for a file.
    """

    _tool_name = "pyright"

    def __init__(
        self,
        pyright_path: str | None = None,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
        db_path: str | None = None,
    ):
        tool = pyright_path or shutil.which("pyright-langserver") or "pyright-langserver"
        super().__init__(tool, cache_size=cache_size, extra_args=extra_args, db_path=db_path)

    def _lsp_command(self) -> list[str]:
        return [self._tool, "--stdio", *self._extra_args]

    def _parse_hover_type(self, hover_text: str) -> str | None:
        """Extract the type part from Pyright's hover markdown."""
        # Find the code block
        match = re.search(r"```python\n(.*?)\n```", hover_text, re.DOTALL)
        if not match:
            return None

        line = match.group(1).strip()
        # line is often "(variable) name: type" or "(function) name: type"
        if ": " in line:
            return line.split(": ", 1)[1].strip()
        return None


# ---------------------------------------------------------------------------
# ty adapter
# ---------------------------------------------------------------------------

class TyAdapter(_LSPTypeOracle):
    """TypeOracle implementation backed by the ty LSP.

    Starts a ty lsp instance and queries individual symbols
    via textDocument/hover to build a comprehensive type index for a file.
    """

    _tool_name = "ty"

    def __init__(
        self,
        ty_path: str | None = None,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
        db_path: str | None = None,
    ):
        tool = ty_path or shutil.which("ty") or "ty"
        super().__init__(tool, cache_size=cache_size, extra_args=extra_args, db_path=db_path)

    def _lsp_command(self) -> list[str]:
        return [self._tool, "lsp", *self._extra_args]

    def _parse_hover_type(self, hover_text: str) -> str | None:
        """Extract the type part from ty's hover markdown."""
        # Try to find a python code block
        match = re.search(r"```python\n(.*?)\n```", hover_text, re.DOTALL)
        if match:
            line = match.group(1).strip()
            if ": " in line:
                return line.split(": ", 1)[1].strip()
            return line

        # Try backticks
        match = re.search(r"`([^`]+)`", hover_text)
        if match:
            return match.group(1).strip()

        # Plain text — return it if non-empty, otherwise None
        stripped = hover_text.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# TypeScript adapter (batch subprocess via Compiler API)
# ---------------------------------------------------------------------------

# Helper Node.js script that uses the TypeScript Compiler API to extract type
# information for all identifiers in a single pass.  This avoids per-symbol
# LSP round-trips, making it significantly faster than an LSP-based approach.
_TS_TYPE_HELPER = """\
"use strict";
var ts;
try { ts = require("typescript"); } catch(e) {
    process.stdout.write("[]"); process.exit(0);
}
var path = require("path");
var filePath = path.resolve(process.argv[2]);
var projectRoot = process.argv[3] || path.dirname(filePath);
var configPath = ts.findConfigFile(projectRoot, ts.sys.fileExists, "tsconfig.json");
var options = {target:ts.ScriptTarget.ES2020, module:ts.ModuleKind.CommonJS,
    allowJs:true, noEmit:true, strict:false, skipLibCheck:true};
if (configPath) {
    try {
        var cf = ts.readConfigFile(configPath, ts.sys.readFile);
        if (cf.config) {
            var pc = ts.parseJsonConfigFileContent(cf.config, ts.sys, path.dirname(configPath));
            Object.assign(options, pc.options);
        }
    } catch(e) {}
}
options.noEmit = true;
var program = ts.createProgram([filePath], options);
var checker = program.getTypeChecker();
var sf = program.getSourceFile(filePath);
if (!sf) { process.stdout.write("[]"); process.exit(0); }
var bindings = [];
var seen = {};
function visit(node) {
    if (ts.isIdentifier(node) && node.text) {
        var p = sf.getLineAndCharacterOfPosition(node.getStart(sf));
        var k = p.line + ":" + p.character;
        if (!seen[k]) {
            seen[k] = true;
            try {
                var sym = checker.getSymbolAtLocation(node);
                if (sym) {
                    var type = checker.getTypeOfSymbolAtLocation(sym, node);
                    var s = checker.typeToString(type, node, ts.TypeFormatFlags.NoTruncation);
                    if (s && s !== "any" && s !== "error") {
                        var kind = "reference";
                        var par = node.parent;
                        if (par && (ts.isVariableDeclaration(par) ||
                            ts.isFunctionDeclaration(par) || ts.isClassDeclaration(par) ||
                            ts.isMethodDeclaration(par) || ts.isParameter(par) ||
                            ts.isPropertyDeclaration(par) || ts.isInterfaceDeclaration(par) ||
                            ts.isTypeAliasDeclaration(par)) && par.name === node)
                            kind = "definition";
                        bindings.push({name:node.text, line:p.line+1, col_start:p.character+1,
                            col_end:p.character+1+node.text.length, type:s, kind:kind});
                    }
                }
            } catch(e) {}
        }
    }
    ts.forEachChild(node, visit);
}
visit(sf);
process.stdout.write(JSON.stringify(bindings));
"""


class TypeScriptAdapter(TypeOracle):
    """TypeOracle implementation using the TypeScript Compiler API via Node.js.

    Runs a helper script that creates a TypeScript program and extracts type
    information for all identifiers in a single pass.  This is significantly
    faster than an LSP-based approach because it avoids per-symbol hover
    round-trips.

    Requires ``node`` on PATH and ``typescript`` installed in the project's
    ``node_modules`` or globally.
    """

    def __init__(
        self,
        node_path: str | None = None,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
        db_path: str | None = None,
    ):
        self._node = node_path or shutil.which("node") or "node"
        self._cache = _FileTypeCache(max_entries=cache_size, db_path=db_path)
        self._extra_args = extra_args or []
        self._script_path: str | None = None

    def is_available(self) -> bool:
        return shutil.which(self._node) is not None

    def _get_script(self) -> str:
        """Write the helper script to a temp file and return its path."""
        if self._script_path and os.path.exists(self._script_path):
            return self._script_path
        fd, path = tempfile.mkstemp(suffix=".js", prefix="emend_ts_types_")
        os.write(fd, _TS_TYPE_HELPER.encode("utf-8"))
        os.close(fd)
        self._script_path = path
        return path

    def infer_file(self, path: Path, project_root: Path | None = None) -> FileTypes:
        path = path.resolve()
        if not path.exists():
            return FileTypes(path=str(path))

        content_hash = _content_hash(path)
        cached = self._cache.get(content_hash)
        if cached is not None:
            return cached

        logger.info("Building type index for %s via TypeScript", path)
        ft = self._run_tsc(path, project_root)
        self._cache.put(content_hash, ft)
        return ft

    def _run_tsc(self, path: Path, project_root: Path | None) -> FileTypes:
        """Run the TypeScript helper script and parse its output."""
        script = self._get_script()
        cwd = str(project_root) if project_root else str(path.parent)
        cmd = [self._node, script, str(path), cwd, *self._extra_args]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, cwd=cwd,
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.debug(
                    "TypeScript helper returned %d: %s",
                    result.returncode, result.stderr[:500] if result.stderr else "",
                )
                return FileTypes(path=str(path))

            bindings_data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
            logger.debug("TypeScript helper failed: %s", exc)
            return FileTypes(path=str(path))

        ft = FileTypes(path=str(path))
        for entry in bindings_data:
            type_desc = parse_type_string(entry["type"])
            ft.bindings.append(TypeBinding(
                name=entry["name"],
                line=entry["line"],
                col_start=entry["col_start"],
                col_end=entry.get("col_end"),
                type_descriptor=type_desc,
                raw_type=entry["type"],
                binding_kind=entry.get("kind", "inferred"),
            ))
        ft.build_index()
        return ft

    def type_at(self, path: Path, line: int, col: int,
                project_root: Path | None = None) -> TypeBinding | None:
        ft = self.infer_file(path, project_root)
        return ft.type_at(line, col)

    def clear_cache(self) -> None:
        self._cache.clear()

    def __del__(self):
        if self._script_path:
            try:
                os.unlink(self._script_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# rust-analyzer adapter
# ---------------------------------------------------------------------------

class RustAnalyzerAdapter(_LSPTypeOracle):
    """TypeOracle implementation backed by the rust-analyzer LSP.

    Starts a rust-analyzer instance and queries individual symbols
    via textDocument/hover to build a type index for Rust source files.
    """

    _tool_name = "rust-analyzer"
    _language_id = "rust"

    def __init__(
        self,
        rust_analyzer_path: str | None = None,
        cache_size: int = 256,
        extra_args: list[str] | None = None,
        db_path: str | None = None,
    ):
        tool = rust_analyzer_path or shutil.which("rust-analyzer") or "rust-analyzer"
        super().__init__(tool, cache_size=cache_size, extra_args=extra_args, db_path=db_path)

    def _lsp_command(self) -> list[str]:
        return [self._tool, *self._extra_args]

    def _parse_hover_type(self, hover_text: str) -> str | None:
        """Extract the type part from rust-analyzer's hover markdown."""
        # rust-analyzer wraps hover in ```rust ... ``` blocks
        match = re.search(r"```rust\n(.*?)\n```", hover_text, re.DOTALL)
        if not match:
            return None

        line = match.group(1).strip()
        # "fn add(a: i32, b: i32) -> i32" -> callable type string
        if line.startswith("fn "):
            return _parse_rust_fn_signature(line)
        # "let x: i32" or "x: Vec<String>" -> type after colon
        if ": " in line:
            return line.rsplit(": ", 1)[1].strip()
        return None


# ---------------------------------------------------------------------------
# Autodetection
# ---------------------------------------------------------------------------

# Config files/sections that indicate a project uses a particular type checker.
_ENGINE_CONFIG_SIGNALS: list[tuple[str, str | None, str]] = [
    # (filename, pyproject.toml section, engine)
    ("pyrefly.toml", "tool.pyrefly", "pyrefly"),
    ("pyrightconfig.json", "tool.pyright", "pyright"),
    ("ty.toml", "tool.ty", "ty"),
]

# File extensions → engine mapping for per-file autodetection.
_EXT_TO_ENGINE: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".rs": "rust-analyzer",
}

# Project-root config files that indicate a language-specific project.
_PROJECT_CONFIG_SIGNALS: list[tuple[str, str]] = [
    ("tsconfig.json", "typescript"),
    ("Cargo.toml", "rust-analyzer"),
]


def _pyproject_has_section(pyproject: Path, dotted_key: str) -> bool:
    """Check if a pyproject.toml contains a ``[dotted.key]`` table."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    # Match [tool.pyright], [tool.ty], etc.  This is a lightweight check —
    # we don't parse full TOML, just look for the section header.
    pattern = r"^\[" + re.escape(dotted_key) + r"\]"
    return bool(re.search(pattern, text, re.MULTILINE))


def detect_type_engine(
    project_root: Path | None = None,
    *,
    file_path: Path | None = None,
) -> str:
    """Detect which type checking engine a project is configured for.

    When *file_path* is given, the file extension is used first to pick
    a language-specific engine (``"typescript"`` for ``.ts``/``.tsx``/``.js``,
    ``"rust-analyzer"`` for ``.rs``).

    For Python projects, detection order (per engine, in priority order:
    pyrefly → pyright → ty):
    1. Standalone config file in the project root (e.g. ``pyrefly.toml``,
       ``pyrightconfig.json``, ``ty.toml``).
    2. Matching ``[tool.*]`` section in ``pyproject.toml``.
    3. First available tool on PATH (pyrefly → ty → pyright).

    Returns the engine name: ``"pyrefly"``, ``"pyright"``, ``"ty"``,
    ``"typescript"``, ``"rust-analyzer"``, or ``"pyrefly"`` as fallback.
    """
    # Per-file extension detection takes priority
    if file_path is not None:
        ext = Path(file_path).suffix.lower()
        engine = _EXT_TO_ENGINE.get(ext)
        if engine:
            return engine

    root = project_root or Path.cwd()

    # Project-root config files for TS/Rust projects
    for filename, engine in _PROJECT_CONFIG_SIGNALS:
        if (root / filename).exists():
            return engine

    # Python engine detection: config file presence
    pyproject = root / "pyproject.toml"
    for filename, pyproject_section, engine in _ENGINE_CONFIG_SIGNALS:
        if (root / filename).exists():
            return engine
        if pyproject_section and pyproject.exists():
            if _pyproject_has_section(pyproject, pyproject_section):
                return engine

    # Python engine detection: tool availability on PATH
    for tool, engine in [("pyrefly", "pyrefly"), ("ty", "ty"), ("pyright", "pyright")]:
        if shutil.which(tool):
            return engine

    # Fallback
    return "pyrefly"


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

_ENGINE_NAMES = ("pyrefly", "pyright", "ty", "typescript", "rust-analyzer", "auto")


def create_type_oracle(
    engine: str = "pyrefly",
    project_root: Path | None = None,
    *,
    file_path: Path | None = None,
    **kwargs,
) -> TypeOracle:
    """Create a TypeOracle instance for the specified engine.

    Args:
        engine: The type inference engine to use.  Defaults to ``"pyrefly"``.
                ``"auto"`` detects the engine from project config files,
                installed tools, and (when given) the target file extension.
                Other choices: ``"pyright"``, ``"ty"``, ``"typescript"``,
                ``"rust-analyzer"``.
        project_root: Project root directory used for config-file detection
                      when *engine* is ``"auto"``.
        file_path: Target file path used for per-file autodetection when
                   *engine* is ``"auto"``.
        **kwargs: Additional keyword arguments passed to the adapter constructor.

    Returns:
        A TypeOracle instance.

    Raises:
        ValueError: If the engine is not recognized.
    """
    if engine == "auto":
        engine = detect_type_engine(project_root, file_path=file_path)

    # Inject disk cache path when not explicitly provided
    if "db_path" not in kwargs:
        kwargs["db_path"] = _type_cache_db_path(project_root)

    if engine == "pyrefly":
        return PyreflyAdapter(**kwargs)
    if engine == "pyright":
        return PyrightAdapter(**kwargs)
    if engine == "ty":
        return TyAdapter(**kwargs)
    if engine == "typescript":
        return TypeScriptAdapter(**kwargs)
    if engine == "rust-analyzer":
        return RustAnalyzerAdapter(**kwargs)
    raise ValueError(
        f"Unknown type inference engine: {engine!r}. "
        f"Supported engines: {', '.join(_ENGINE_NAMES)}"
    )
