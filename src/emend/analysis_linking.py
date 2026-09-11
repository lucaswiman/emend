"""Pure project linking for cached, file-local analysis facts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
import posixpath

from emend.analysis_snapshot import ExtractedFile, FileRevision


_FINAL_RELATIONS = (
    "fg_refs",
    "calls",
    "calls_by_callee",
    "calls_by_file",
    "ref_by_block",
    "noncall_private_member_refs",
    "module_level_refs",
    "method_calls",
)
_SOURCE_EXTENSIONS = (".pyi", ".tsx", ".jsx", ".py", ".ts", ".js", ".rs")


def _normalize_qn(value: str) -> str:
    """Normalize a resolved identity; callers must resolve relative syntax first."""
    value = (
        value.strip("'\"")
        .replace("::", ".")
        .replace("/", ".")
        .replace("\\", ".")
    )
    return ".".join(part for part in value.split(".") if part)


def _strip_import_extension(value: str) -> str:
    for extension in _SOURCE_EXTENSIONS:
        if value.endswith(extension):
            return value[: -len(extension)]
    return value


class ModuleCatalog:
    """Immutable lookup from import spellings to snapshot module identities."""

    def __init__(self, modules: Iterable[str] = ()) -> None:
        normalized = frozenset(
            filter(None, (_normalize_qn(module) for module in modules))
        )
        aliases = {module: module for module in normalized}
        self.modules = normalized
        self._aliases = aliases
        self._language_aliases: dict[str, dict[str, str]] = {}
        self._rust_roots: tuple[str, ...] = ()

    @classmethod
    def from_revisions(cls, revisions: Iterable[FileRevision]) -> "ModuleCatalog":
        revisions = tuple(revisions)
        catalog = cls(revision.module_name for revision in revisions)
        rust_roots: list[str] = []
        for revision in revisions:
            stem = PurePosixPath(revision.file_path).stem
            module = _normalize_qn(revision.module_name)
            aliases = catalog._language_aliases.setdefault(revision.language, {})
            aliases[module] = module
            if revision.language in ("typescript", "javascript") and stem == "index":
                alias = module.removesuffix(".index")
                if alias:
                    aliases.setdefault(alias, module)
            elif revision.language == "rust" and stem in ("lib", "main"):
                rust_roots.append(module)
        catalog._rust_roots = tuple(sorted(rust_roots, key=lambda root: root != "lib"))
        return catalog

    def _matching_alias(self, normalized: str, language: str) -> str:
        aliases = self._language_aliases.get(language, self._aliases)
        prefix = normalized
        while prefix and prefix not in aliases:
            prefix = prefix.rpartition(".")[0]
        return prefix

    def resolve(self, candidate: str, language: str = "") -> str:
        """Return the snapshot's canonical spelling, or a stable external QN."""
        normalized = _normalize_qn(candidate)
        aliases = self._language_aliases.get(language, self._aliases)
        alias = self._matching_alias(normalized, language)
        return f"{aliases[alias]}{normalized[len(alias):]}" if alias else normalized

    def represents(self, candidate: str, language: str = "") -> bool:
        return bool(self._matching_alias(_normalize_qn(candidate), language))


def _lexical_suffix(lexical: str, local_name: str) -> str:
    lexical_qn = _normalize_qn(lexical)
    local_qn = _normalize_qn(local_name)
    if lexical_qn == local_qn:
        return ""
    prefix = f"{local_qn}."
    return lexical_qn[len(prefix):] if lexical_qn.startswith(prefix) else ""


def _python_module(raw: str, importing_file: str, module_name: str) -> str | None:
    level = len(raw) - len(raw.lstrip("."))
    if level == 0:
        return raw
    package = _normalize_qn(module_name).split(".")
    if PurePosixPath(importing_file).stem != "__init__":
        package = package[:-1]
    if level > len(package):
        return None
    keep = max(0, len(package) - level + 1)
    tail = raw[level:].replace(".", "/")
    return ".".join([*package[:keep], *filter(None, tail.split("/"))])


def _typescript_module(raw: str, module_name: str) -> str | None:
    raw = _strip_import_extension(raw)
    if not raw.startswith(("./", "../")):
        return raw
    current = module_name.replace("::", "/").replace(".", "/")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(current), raw))
    return None if resolved == ".." or resolved.startswith("../") else resolved


def _rust_module(
    raw: str, module_name: str, catalog: ModuleCatalog
) -> str | None:
    parts = [part for part in raw.split("::") if part]
    current = _normalize_qn(module_name).split(".")
    if not parts:
        return ""
    if parts[0] == "crate":
        tail = ".".join(parts[1:])
        if tail:
            if catalog.represents(tail, "rust"):
                return catalog.resolve(tail, "rust")
            root = catalog._rust_roots[0] if catalog._rust_roots else "crate"
            return f"{root}.{tail}"
        return catalog._rust_roots[0] if catalog._rust_roots else "crate"
    if parts[0] == "self":
        return ".".join([*current, *parts[1:]])
    supers = 0
    while supers < len(parts) and parts[supers] == "super":
        supers += 1
    if supers:
        if ".".join(current) in catalog._rust_roots:
            return None
        keep = max(0, len(current) - supers)
        return ".".join([*current[:keep], *parts[supers:]])
    return raw


def _import_target(row: list[object], lexical: str, catalog: ModuleCatalog) -> str:
    (
        importing_file, _binding_id, local_name, raw_module, imported_name,
        is_star, _scope_id, _scope_start, _scope_end, _byte_offset, _line,
        language, module_name,
    ) = row
    raw = str(raw_module)
    language = str(language)
    if language == "python":
        plain_root = raw.split(".", 1)[0]
        module = _python_module(
            plain_root if not imported_name and local_name == plain_root else raw,
            str(importing_file),
            str(module_name),
        )
    elif language in ("typescript", "javascript"):
        module = _typescript_module(raw, str(module_name))
    elif language == "rust":
        imported = str(imported_name) if imported_name and not is_star else ""
        module = _rust_module("::".join(filter(None, (raw, imported))), str(module_name), catalog)
        imported_name = ""
    else:
        module = raw
    if module is None:
        return _normalize_qn(lexical)
    module = catalog.resolve(module, language)
    suffix = _lexical_suffix(lexical, str(local_name))
    parts = [module]
    if imported_name and not is_star and str(imported_name) != "*":
        parts.append(str(imported_name))
    if suffix:
        parts.append(suffix)
    return _normalize_qn(".".join(filter(None, parts)))


def _copy_rows(rows: dict[str, list[list[object]]]) -> dict[str, list[list[object]]]:
    copied = {name: [list(row) for row in relation] for name, relation in rows.items()}
    for name in _FINAL_RELATIONS:
        copied[name] = []
    return copied


def _link_file(extracted: ExtractedFile, catalog: ModuleCatalog) -> ExtractedFile:
    rows = _copy_rows(extracted.rows)
    imports = {str(row[1]): row for row in rows.get("local_imports", ())}
    definitions = {
        (str(row[0]), int(row[4])) for row in rows.get("fg_sym", ())
    }

    for local_ref in rows.get("local_refs", ()):
        (
            file_path,
            lexical_qn,
            local_qn,
            target_kind,
            binding_id,
            kind,
            line,
            col,
            func_qn,
            block_id,
            caller_qn,
            _start_byte,
        ) = local_ref
        if target_kind in ("local", "builtin"):
            target = _normalize_qn(str(local_qn)) or _normalize_qn(str(lexical_qn))
        elif target_kind == "import":
            binding = imports.get(str(binding_id))
            target = (
                _import_target(binding, str(lexical_qn), catalog)
                if binding is not None
                else _normalize_qn(str(lexical_qn))
            )
        elif target_kind == "unresolved":
            target = _normalize_qn(str(lexical_qn))
        else:
            raise ValueError(f"unknown local reference target kind: {target_kind!r}")

        line = int(line)
        col = int(col)
        block_id = int(block_id)
        func_qn = str(func_qn)
        kind = str(kind)
        caller_qn = str(caller_qn) or _normalize_qn(extracted.revision.module_name)
        reference = [target, file_path, line, col, kind, func_qn, block_id]
        rows["fg_refs"].append(reference)

        definition_site = (target, line) in definitions
        if func_qn and block_id >= 0 and not definition_site:
            rows["ref_by_block"].append([file_path, func_qn, block_id, target])
            member = target.rsplit(".", 1)[-1]
            if (
                kind != "call"
                and "." in target
                and member.startswith("_")
                and not member.startswith("__")
            ):
                rows["noncall_private_member_refs"].append(
                    [file_path, func_qn, block_id, member]
                )
        else:
            rows["module_level_refs"].append([target, file_path, line])

        if kind != "call":
            continue
        call = [caller_qn, target, file_path, line, col, func_qn, block_id]
        rows["calls"].append(call)
        rows["calls_by_callee"].append(
            [target, caller_qn, file_path, line, col, func_qn, block_id]
        )
        rows["calls_by_file"].append(
            [file_path, caller_qn, target, line, col, func_qn, block_id]
        )
        lexical = _normalize_qn(str(lexical_qn))
        if "." in lexical:
            receiver, method = lexical.rsplit(".", 1)
            rows["method_calls"].append(
                [
                    file_path,
                    func_qn or "<module>",
                    receiver.rsplit(".", 1)[-1],
                    method,
                    block_id if func_qn and block_id >= 0 else 0,
                    line - 1,
                ]
            )

    return ExtractedFile(
        revision=extracted.revision,
        qnames=extracted.qnames,
        rows=rows,
    )


def link_extracted_files(
    files: Iterable[ExtractedFile], catalog: ModuleCatalog
) -> list[ExtractedFile]:
    """Link cached local facts without mutating inputs or reading project state."""
    return [_link_file(extracted, catalog) for extracted in files]
