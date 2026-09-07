"""Immutable, file-independent views of native symbol records.

The syntax collector and scope resolver have different contracts (notably
signature whitespace and parameter symbols). Normalize their records here,
without pretending those two native inventories are interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace


@dataclass(frozen=True, slots=True)
class SymbolProjection:
    name: str
    kind: str
    path: tuple[str, ...] = ()
    line: int = 0
    end_line: int = 0
    col_offset: int = 0
    signature: str | None = None
    type_annotation: str | None = None
    returns: str | None = None
    decorators: tuple[str, ...] = ()
    decorator_line_start: int | None = None
    param_names: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    children: tuple['SymbolProjection', ...] = ()

    @classmethod
    def from_dict(cls, record: dict) -> 'SymbolProjection':
        values = {name: record[name] for name in _FIELDS if name in record}
        for name in ("path", "decorators", "param_names", "bases"):
            values[name] = tuple(record.get(name) or ())
        if "path" not in record:
            values["path"] = (record["name"],)
        values["children"] = project_symbols(record.get("children", ()))
        values["parameters"] = tuple(_extract_params_from_signature(record.get("signature")))
        return cls(**values)

    @property
    def local_name(self) -> str:
        return ".".join(self.path)

    def walk(self, depth=1, parent=None):
        """Lookup order and nearest enclosing class, excluding references."""
        if self.kind != "reference":
            yield self, depth, parent
            for child in self.children:
                yield from child.walk(depth + 1, self.name if self.kind == "class" else parent)

    def nested(self, max_depth=None):
        from emend.component_selector import NestedSymbol

        return NestedSymbol(
            self.name, self.kind, self.line, self.end_line, self.col_offset,
            list(self.path), list(self.decorators), self.decorator_line_start,
            list(self.param_names),
            [child.nested(None if max_depth is None else max_depth - 1)
             for child in self.children if child.kind not in ("variable", "reference")]
            if max_depth is None or max_depth > 0 else [],
        )


_FIELDS = frozenset(field.name for field in fields(SymbolProjection))


def project_symbols(records) -> tuple[SymbolProjection, ...]:
    return tuple(SymbolProjection.from_dict(record) for record in records)


def symbol_hierarchy(records, module_path: str, separator: str = "."):
    """Localize flat or nested resolver paths and attach missing-parent roots."""
    prefix = tuple(module_path.split(separator))
    symbols = {}

    def flatten(items):
        for item in items:
            yield item
            yield from flatten(item.children)

    for symbol in flatten(project_symbols(records)):
        path = symbol.path
        if path[:len(prefix)] == prefix:
            path = path[len(prefix):]
        if path:
            symbols[path] = replace(symbol, name=path[-1], path=path, children=())
    children = {path: [] for path in symbols}
    roots = []
    for path in sorted(symbols, key=len):
        (children[path[:-1]] if path[:-1] in symbols else roots).append(path)

    def build(path):
        return replace(symbols[path], children=tuple(build(child) for child in children[path]))

    return tuple(build(path) for path in roots)


@dataclass
class SymbolInfo:
    """Information about a discovered symbol."""

    path: str  # Full selector path like "file.py::Class.method"
    name: str
    kind: str  # 'class', 'function', 'async_function', 'method', 'async_method'
    line: int
    end_line: int
    decorators: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    returns: str | None = None
    parent: str | None = None
    bases: list[str] = field(default_factory=list)
    depth: int = 1

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        required = ("path", "name", "kind", "line", "end_line")
        optional = ("decorators", "parameters", "returns", "parent", "bases")
        return {name: getattr(self, name) for name in (*required, *optional)
                if name in required or getattr(self, name)}


def _extract_params_from_signature(signature: str | None) -> list[str]:
    """Parse parameter strings from Rust signature like '(x: int, y, *args, **kwargs) -> str'."""
    if not signature:
        return []
    sig = signature.partition(" -> ")[0].strip()
    if sig.startswith("(") and sig.endswith(")"):
        sig = sig[1:-1]
    # Split on top-level commas only — a comma inside brackets/parens (e.g. in
    # ``b: Dict[str, int]`` or a default like ``x=(1, 2)``) is part of a single
    # parameter and must not split it.
    params: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in sig:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            params.append("".join(current))
            current = []
            continue
        current.append(ch)
    params.append("".join(current))
    return [p.strip() for p in params if p.strip()]


def _symbol_info_view(symbols, filepath, depth=1, parent=None):
    return [
        SymbolInfo(
            path=f"{filepath}::{sym.local_name}", name=sym.name, kind=sym.kind,
            line=sym.line, end_line=sym.end_line,
            decorators=[f"@{dec}" for dec in sym.decorators],
            parameters=list(sym.parameters),
            returns=sym.returns, parent=enclosing_class,
            bases=list(sym.bases), depth=level,
        )
        for root in symbols
        for sym, level, enclosing_class in root.walk(depth, parent)
    ]
