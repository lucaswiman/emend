"""Mapping store for cross-service identifier mappings and module mappings.

Provides two capabilities:
1. **Identifier mappings** — records that an identifier in one project
   maps to an identifier in another (e.g. ``users.UserService.create``
   → ``POST /api/v1/users`` in the gateway repo).
2. **Module mappings** — records that a dotted module prefix (e.g.
   ``payments``) maps to a local directory or GitHub repo, enabling
   cross-project symbol resolution.

Both are stored in ``<project>/.emend/mappings.yaml`` as human-readable
YAML, suitable for version control.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class IdentifierMapping:
    """A mapping between identifiers across services/repos."""

    source_project: str
    source_identifier: str
    source_kind: str = ""  # function, class, endpoint, model, field, ...
    target_project: str = ""
    target_identifier: str = ""
    target_kind: str = ""
    relationship: str = "equivalent"  # equivalent, calls, implements, produces, consumes
    confidence: float = 1.0  # 0–1, useful for heuristic/LLM-generated
    provenance: str = "manual"  # manual, heuristic, llm
    evidence: str = ""  # human-readable explanation
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModuleMapping:
    """A mapping from a dotted module prefix to a repo/directory."""

    module_prefix: str
    repo: str = ""  # GitHub repo (org/name), cloned on demand via gh
    local_path: str = ""  # alternative: a local directory
    branch: str = ""  # optional branch/tag for gh clone
    subpath: str = ""  # subdirectory within the repo (e.g. "src/payments")
    provenance: str = "manual"  # manual, heuristic, llm
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _mappings_yaml_path(project_root: str | Path) -> Path:
    """Return the path to the mappings YAML file."""
    from .transform import _knowledge_db_dir
    return _knowledge_db_dir(project_root) / "mappings.yaml"


def _load_yaml(path: Path) -> dict:
    """Load YAML from path, returning empty dict on missing/error."""
    if not path.is_file():
        return {}
    try:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_yaml(path: Path, data: dict) -> None:
    """Save data as YAML to path, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _identifier_mapping_to_yaml(m: IdentifierMapping) -> dict:
    """Serialize an IdentifierMapping to a YAML-friendly dict."""
    d: dict[str, Any] = {
        "source_project": m.source_project,
        "source_identifier": m.source_identifier,
    }
    # Only include non-default fields to keep YAML clean
    if m.source_kind:
        d["source_kind"] = m.source_kind
    d["target_project"] = m.target_project
    d["target_identifier"] = m.target_identifier
    if m.target_kind:
        d["target_kind"] = m.target_kind
    if m.relationship != "equivalent":
        d["relationship"] = m.relationship
    if m.confidence != 1.0:
        d["confidence"] = m.confidence
    if m.provenance != "manual":
        d["provenance"] = m.provenance
    if m.evidence:
        d["evidence"] = m.evidence
    if m.metadata:
        d["metadata"] = m.metadata
    return d


def _yaml_to_identifier_mapping(d: dict) -> IdentifierMapping:
    """Deserialize a dict from YAML into an IdentifierMapping."""
    return IdentifierMapping(
        source_project=d.get("source_project", ""),
        source_identifier=d.get("source_identifier", ""),
        source_kind=d.get("source_kind", ""),
        target_project=d.get("target_project", ""),
        target_identifier=d.get("target_identifier", ""),
        target_kind=d.get("target_kind", ""),
        relationship=d.get("relationship", "equivalent"),
        confidence=d.get("confidence", 1.0),
        provenance=d.get("provenance", "manual"),
        evidence=d.get("evidence", ""),
        metadata=d.get("metadata", {}),
    )


def _module_mapping_to_yaml(m: ModuleMapping) -> dict:
    """Serialize a ModuleMapping to a YAML-friendly dict."""
    d: dict[str, Any] = {"module_prefix": m.module_prefix}
    if m.repo:
        d["repo"] = m.repo
    if m.local_path:
        d["path"] = m.local_path
    if m.branch:
        d["branch"] = m.branch
    if m.subpath:
        d["subpath"] = m.subpath
    if m.provenance != "manual":
        d["provenance"] = m.provenance
    if m.metadata:
        d["metadata"] = m.metadata
    return d


def _yaml_to_module_mapping(d: dict) -> ModuleMapping:
    """Deserialize a dict from YAML into a ModuleMapping."""
    return ModuleMapping(
        module_prefix=d.get("module_prefix", ""),
        repo=d.get("repo", ""),
        local_path=d.get("path", d.get("local_path", "")),
        branch=d.get("branch", ""),
        subpath=d.get("subpath", ""),
        provenance=d.get("provenance", "manual"),
        metadata=d.get("metadata", {}),
    )


# ---------------------------------------------------------------------------
# MappingStore (replaces KnowledgeBase)
# ---------------------------------------------------------------------------


def make_resolve_module_cb(
    store: "MappingStore",
) -> Callable[[str, int, str], Optional[str]]:
    """Create a resolve_module_cb suitable for resolve_through_reexports().

    Handles relative imports by walking up directories, and absolute imports
    by delegating to store.resolve_module_to_path().
    """
    def resolve_module_cb(module: str, level: int, current_file: str) -> str | None:
        if level > 0:
            current_path = Path(current_file).resolve().parent
            for _ in range(level - 1):
                current_path = current_path.parent
            if not module:
                return str(current_path) if current_path.is_dir() else None
            target = current_path
            for p in module.split('.'):
                target = target / p
            if target.with_suffix('.py').is_file():
                return str(target.with_suffix('.py'))
            if target.is_dir():
                return str(target)
            return None
        try:
            return store.resolve_module_to_path(module)
        except Exception:
            return None

    return resolve_module_cb


def resolve_dotted_path_to_selector(
    resolved_base: str,
    rem_parts: list[str],
    resolve_module_cb: Callable[[str, int, str], Optional[str]]
) -> str | None:
    """Walk remaining dotted parts against a base directory or file.

    Handles snake_case fallback and __init__.py re-exports.
    """
    # If the mapped part is a file, the rest are symbols
    if os.path.isfile(resolved_base):
        return f"{resolved_base}::{'.'.join(rem_parts)}" if rem_parts else resolved_base

    # If it was a directory, walk the remaining parts recursively.
    if os.path.isdir(resolved_base):
        current_path = Path(resolved_base)

        if not rem_parts:
            return str(current_path)

        for j, part in enumerate(rem_parts):
            names = [part]
            # Robust snake_case translation
            snake = _to_snake_case(part)
            if snake != part:
                names.append(snake)

            found = False
            # 1. Try as directory first
            for name in names:
                if (current_path / name).is_dir():
                    current_path = current_path / name
                    found = True
                    break
            if found:
                if j == len(rem_parts) - 1:
                    # Consumed all parts as directories
                    return str(current_path)
                continue

            # 2. Try as file
            for name in names:
                candidate_file = current_path / (name + ".py")
                if candidate_file.is_file():
                    # Found the file!
                    symbol_parts = rem_parts[j+1:]
                    # Heuristic: if we matched a snake_case name and there are no remaining parts,
                    # the original part might be a symbol in this file.
                    if name != part and not symbol_parts:
                        symbol_parts = [part]

                    symbol_suffix = ".".join(symbol_parts)
                    if symbol_suffix:
                        return f"{candidate_file}::{symbol_suffix}"
                    return str(candidate_file)

            # 3. Neither file nor dir found - check for re-export in __init__.py
            init_file = current_path / "__init__.py"
            if init_file.is_file():
                from emend.ast_utils import resolve_through_reexports

                res = resolve_through_reexports(
                    str(init_file), part, resolve_module_cb
                )
                if res:
                    target_file, _ = res
                    # Re-construct the symbol path
                    symbol_parts = [part] + rem_parts[j+1:]
                    symbol_suffix = ".".join(symbol_parts)
                    return f"{target_file}::{symbol_suffix}"

                # Fallback: just point to __init__.py with symbols
                return f"{init_file}::{'.'.join(rem_parts[j:])}"

            # Completely stuck
            break

    return None


def _to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case robustly."""
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)).lower()


class MappingStore:
    """Interface to the mappings YAML file.

    Usage::

        store = MappingStore(".")          # project root
        store.add_module_mapping(ModuleMapping(...))
        results = store.list_module_mappings()
        store.close()
    """

    def __init__(self, project_root: str = ".") -> None:
        self._yaml_path = _mappings_yaml_path(project_root)

        # Migrate from old SQLite knowledge.db if it exists and YAML doesn't.
        if not self._yaml_path.is_file():
            self._migrate_from_sqlite(project_root)

        self._data = _load_yaml(self._yaml_path)

    def _migrate_from_sqlite(self, project_root: str) -> None:
        """One-time migration from knowledge.db to mappings.yaml."""
        from .transform import _knowledge_db_dir
        db_path = _knowledge_db_dir(project_root) / "knowledge.db"
        if not db_path.is_file():
            return

        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            data: dict[str, Any] = {}

            # Migrate identifier_mappings
            try:
                rows = conn.execute(
                    "SELECT * FROM identifier_mapping WHERE deleted = 0 "
                    "ORDER BY confidence DESC, updated_at DESC"
                ).fetchall()
                if rows:
                    mappings = []
                    for row in rows:
                        m = IdentifierMapping(
                            source_project=row["source_project"],
                            source_identifier=row["source_identifier"],
                            source_kind=row["source_kind"],
                            target_project=row["target_project"],
                            target_identifier=row["target_identifier"],
                            target_kind=row["target_kind"],
                            relationship=row["relationship"],
                            confidence=row["confidence"],
                            provenance=row["provenance"],
                            evidence=row["evidence"],
                        )
                        try:
                            import json
                            m.metadata = json.loads(row["metadata_json"])
                        except Exception:
                            pass
                        mappings.append(_identifier_mapping_to_yaml(m))
                    data["identifier_mappings"] = mappings
            except Exception:
                pass

            # Migrate module_mappings
            try:
                rows = conn.execute(
                    "SELECT * FROM module_mapping WHERE deleted = 0 "
                    "ORDER BY length(module_prefix) DESC"
                ).fetchall()
                if rows:
                    modules = []
                    for row in rows:
                        m = ModuleMapping(
                            module_prefix=row["module_prefix"],
                            repo=row["repo"],
                            local_path=row["local_path"],
                            branch=row["branch"],
                            subpath=row["subpath"],
                            provenance=row["provenance"],
                        )
                        try:
                            import json
                            m.metadata = json.loads(row["metadata_json"])
                        except Exception:
                            pass
                        modules.append(_module_mapping_to_yaml(m))
                    data["module_mappings"] = modules
            except Exception:
                pass

            conn.close()

            if data:
                _save_yaml(self._yaml_path, data)

        except Exception:
            pass  # Migration is best-effort

    def _save(self) -> None:
        """Persist current state to YAML."""
        _save_yaml(self._yaml_path, self._data)

    @property
    def yaml_path(self) -> Path:
        return self._yaml_path

    def close(self) -> None:
        """No-op for API compatibility."""
        pass

    # -- Identifier mappings -------------------------------------------------

    def add_mapping(self, m: IdentifierMapping) -> None:
        """Add an identifier mapping."""
        mappings = self._data.setdefault("identifier_mappings", [])
        mappings.append(_identifier_mapping_to_yaml(m))
        self._save()

    def delete_mapping(
        self,
        source_identifier: str,
        *,
        source_project: str | None = None,
        target_identifier: str | None = None,
    ) -> bool:
        """Delete identifier mappings matching the given criteria.

        Returns True if any mappings were removed.
        """
        mappings = self._data.get("identifier_mappings", [])
        original_len = len(mappings)
        filtered = []
        for m in mappings:
            if m.get("source_identifier") != source_identifier:
                filtered.append(m)
                continue
            if source_project and m.get("source_project") != source_project:
                filtered.append(m)
                continue
            if target_identifier and m.get("target_identifier") != target_identifier:
                filtered.append(m)
                continue
            # Match — skip this entry (delete it)
        self._data["identifier_mappings"] = filtered
        if len(filtered) < original_len:
            self._save()
            return True
        return False

    def search_mappings(
        self,
        query: str,
        *,
        source_project: str | None = None,
        target_project: str | None = None,
        relationship: str | None = None,
        limit: int = 50,
    ) -> list[IdentifierMapping]:
        """Search identifier mappings by substring match."""
        results: list[IdentifierMapping] = []
        query_lower = query.lower()
        for d in self._data.get("identifier_mappings", []):
            if source_project and d.get("source_project") != source_project:
                continue
            if target_project and d.get("target_project") != target_project:
                continue
            if relationship and d.get("relationship", "equivalent") != relationship:
                continue
            if query_lower:
                searchable = " ".join([
                    d.get("source_identifier", ""),
                    d.get("target_identifier", ""),
                    d.get("evidence", ""),
                ]).lower()
                if query_lower not in searchable:
                    continue
            results.append(_yaml_to_identifier_mapping(d))
            if len(results) >= limit:
                break
        return results

    def list_mappings(
        self,
        *,
        source_project: str | None = None,
        target_project: str | None = None,
        relationship: str | None = None,
        limit: int = 100,
    ) -> list[IdentifierMapping]:
        """List identifier mappings with optional filters."""
        return self.search_mappings(
            "",
            source_project=source_project,
            target_project=target_project,
            relationship=relationship,
            limit=limit,
        )

    def find_mappings_for(
        self,
        identifier: str,
        *,
        project: str | None = None,
        direction: str = "both",  # source, target, both
    ) -> list[IdentifierMapping]:
        """Find all mappings where *identifier* appears as source or target."""
        results: list[IdentifierMapping] = []
        for d in self._data.get("identifier_mappings", []):
            match = False
            if direction in ("source", "both"):
                if d.get("source_identifier") == identifier:
                    if not project or d.get("source_project") == project:
                        match = True
            if direction in ("target", "both"):
                if d.get("target_identifier") == identifier:
                    if not project or d.get("target_project") == project:
                        match = True
            if match:
                results.append(_yaml_to_identifier_mapping(d))
        return results

    # -- Module mappings -----------------------------------------------------

    def add_module_mapping(self, m: ModuleMapping) -> None:
        """Add a module mapping. Replaces an existing one with the same prefix."""
        modules = self._data.setdefault("module_mappings", [])
        # Replace existing mapping with same prefix if present
        for i, existing in enumerate(modules):
            if existing.get("module_prefix") == m.module_prefix:
                modules[i] = _module_mapping_to_yaml(m)
                self._save()
                return
        modules.append(_module_mapping_to_yaml(m))
        self._save()

    def get_module_mapping_by_prefix(self, prefix: str) -> ModuleMapping | None:
        """Look up a module mapping by its exact prefix string."""
        for d in self._data.get("module_mappings", []):
            if d.get("module_prefix") == prefix:
                return _yaml_to_module_mapping(d)
        return None

    def delete_module_mapping_by_prefix(self, prefix: str) -> bool:
        """Delete a module mapping by its prefix string."""
        modules = self._data.get("module_mappings", [])
        for i, d in enumerate(modules):
            if d.get("module_prefix") == prefix:
                modules.pop(i)
                self._save()
                return True
        return False

    def update_module_mapping(self, prefix: str, **kwargs: Any) -> bool:
        """Update fields on an existing module mapping by prefix."""
        modules = self._data.get("module_mappings", [])
        for d in modules:
            if d.get("module_prefix") != prefix:
                continue
            for key, value in kwargs.items():
                if key == "local_path":
                    d["path"] = value
                elif key == "metadata":
                    if value:
                        d["metadata"] = value
                    else:
                        d.pop("metadata", None)
                elif value:
                    d[key] = value
                else:
                    d.pop(key, None)
            self._save()
            return True
        return False

    def list_module_mappings(self) -> list[ModuleMapping]:
        """List all module mappings ordered by prefix length (longest first)."""
        modules = self._data.get("module_mappings", [])
        # Sort by prefix length descending
        sorted_modules = sorted(
            modules, key=lambda d: len(d.get("module_prefix", "")), reverse=True
        )
        return [_yaml_to_module_mapping(d) for d in sorted_modules]

    def resolve_module(self, module_name: str) -> ModuleMapping | None:
        """Find the best (longest-prefix) module mapping for *module_name*.

        For example, if there are mappings for ``payments`` and
        ``payments.models``, and *module_name* is ``payments.models.User``,
        the ``payments.models`` mapping wins.
        """
        best: ModuleMapping | None = None
        best_len = -1
        for d in self._data.get("module_mappings", []):
            prefix = d.get("module_prefix", "")
            if module_name == prefix or module_name.startswith(prefix + "."):
                if len(prefix) > best_len:
                    best = _yaml_to_module_mapping(d)
                    best_len = len(prefix)
        return best

    def resolve_module_to_path(
        self, module_name: str, *, cache_dir: str | None = None
    ) -> str | None:
        """Resolve a module name to a local file path.

        If the mapping points to a GitHub repo and it hasn't been cloned yet,
        clones it via ``gh repo clone`` into *cache_dir* (defaults to
        ``~/.cache/emend/repo-checkouts/{repo_id}/``).

        Returns the resolved directory/file path, or None if no mapping found.
        """
        mm = self.resolve_module(module_name)
        if mm is None:
            return None

        # Determine the local root for this mapping.
        if mm.local_path:
            local_root = mm.local_path
        elif mm.repo:
            local_root = _ensure_repo_cloned(
                mm.repo, branch=mm.branch, cache_dir=cache_dir,
            )
        else:
            return None

        # Convert the remaining module suffix to a path.
        suffix = module_name
        if suffix == mm.module_prefix:
            suffix = ""
        elif suffix.startswith(mm.module_prefix + "."):
            suffix = suffix[len(mm.module_prefix) + 1:]

        base = Path(local_root)
        if mm.subpath:
            base = base / mm.subpath

        if suffix:
            parts = suffix.split(".")
            # Try as a package (directory with __init__.py) first, then module.
            candidate_dir = base / "/".join(parts)
            if candidate_dir.is_dir():
                return str(candidate_dir)

            # Try original name and snake_case variant for the file
            names_to_try = [parts[-1]]
            snake = _to_snake_case(parts[-1])
            if snake != parts[-1]:
                names_to_try.append(snake)

            parent_dir = base / "/".join(parts[:-1])
            if parent_dir.is_dir():
                for name in names_to_try:
                    candidate_file = parent_dir / (name + ".py")
                    if candidate_file.is_file():
                        return str(candidate_file)

            # Fall back to the directory for the dotted prefix if it exists.
            return str(candidate_dir) if candidate_dir.is_dir() else None
        else:
            return str(base) if base.exists() else None

    def resolve_selector(self, selector: str) -> str | None:
        """Resolve a dotted selector using module mappings.

        If the selector is already an explicit selector (contains ::) or
        a file path, it is returned as-is.

        If it's a dotted selector like 'a.b.C', it tries to find a module
        mapping for 'a.b' or 'a', and returns an explicit selector like
        'path/to/a/b.py::C'.
        """
        from emend.component_selector import parse_extended_selector

        if "::" in selector:
            return selector

        try:
            sel = parse_extended_selector(selector)
            if sel.file_path:
                return selector

            if not sel.symbol_path:
                return None

            parts = sel.symbol_path
            # Find the best (longest-prefix) module mapping for the whole path
            mm = self.resolve_module(selector)
            if not mm:
                return None

            # Resolve the mapped part to a path
            resolved_base = self.resolve_module_to_path(mm.module_prefix)
            if not resolved_base:
                return None

            # Determine remaining parts after the mapped prefix
            prefix_parts = mm.module_prefix.split('.')
            rem_parts = parts[len(prefix_parts):]

            return resolve_dotted_path_to_selector(
                resolved_base,
                rem_parts,
                resolve_module_cb=make_resolve_module_cb(self)
            )
        except Exception:
            return None

    def fetch_module_repo(self, module_prefix: str) -> str | None:
        """Force-fetch the latest commits for a module mapping's repo.

        Returns the worktree path, or None if the mapping has no repo.
        """
        mm = self.get_module_mapping_by_prefix(module_prefix)
        if mm is None or not mm.repo:
            return None
        local_root = _ensure_repo_cloned(mm.repo, branch=mm.branch)
        root = _repo_checkouts_root()
        rid = _repo_id(mm.repo)
        contents_dir = root / rid / "contents"
        ref = mm.branch or _default_branch(contents_dir) or "main"
        _maybe_fetch_branch(contents_dir, Path(local_root), ref, force=True)
        return local_root


# Backward compatibility alias
KnowledgeBase = MappingStore


# ---------------------------------------------------------------------------
# Repo checkout helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _global_cache_dir() -> Path:
    """Return the global emend cache directory.

    Checks ``EMEND_CACHE_DIR`` environment variable first, then falls back
    to ``~/.cache/emend``.
    """
    env = os.environ.get("EMEND_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "emend"


def _repo_checkouts_root(cache_dir: str | None = None) -> Path:
    """Return the global repo-checkouts directory.

    Layout::

        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/contents   — bare clone
        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/checkouts/{ref} — worktrees
    """
    if cache_dir:
        return Path(cache_dir)
    return _global_cache_dir() / "repo-checkouts"


def _repo_id(repo: str) -> str:
    """Normalize a repo identifier for use as a directory name.

    ``org/name`` → ``org--name`` (avoids nested directories).
    """
    return repo.replace("/", "--")


def _ensure_repo_cloned(
    repo: str,
    *,
    branch: str = "",
    cache_dir: str | None = None,
) -> str:
    """Clone a GitHub repo and check out a worktree for the requested ref.

    Layout::

        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/contents        — bare clone
        {EMEND_CACHE_DIR}/repo-checkouts/{repo_id}/checkouts/{ref}  — worktree

    Returns the path to the worktree.
    """
    root = _repo_checkouts_root(cache_dir)
    rid = _repo_id(repo)
    contents_dir = root / rid / "contents"

    # --- Step 1: bare clone into contents/ if not already present ---
    if not (contents_dir / "HEAD").exists():
        contents_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["gh", "repo", "clone", repo, str(contents_dir), "--", "--bare"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise RuntimeError(
                "'gh' CLI not found. Install GitHub CLI to clone external repos: "
                "https://cli.github.com/"
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to clone {repo}: {e.stderr.strip()}")

    # Fetch tags — bare clones and gh may not include them by default.
    try:
        subprocess.run(
            ["git", "fetch", "origin", "--tags"],
            cwd=str(contents_dir),
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # best-effort; tags may already be present

    # --- Step 2: determine the ref to check out ---
    ref = branch or _default_branch(contents_dir)
    if not ref:
        ref = "main"

    # --- Step 3: create or reuse a worktree for this ref ---
    checkouts_dir = root / rid / "checkouts"
    # Sanitize ref for directory name (e.g. "v1.2.3", "feature/foo" → safe name).
    safe_ref = ref.replace("/", "--")
    worktree_dir = checkouts_dir / safe_ref

    if worktree_dir.is_dir():
        # For tags, always reuse (immutable).
        # For branches, check if stale (older than TTL).
        if not _is_tag(contents_dir, ref):
            _maybe_fetch_branch(contents_dir, worktree_dir, ref, ttl_hours=24)
        return str(worktree_dir)

    checkouts_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "worktree", "add", str(worktree_dir), ref]
    try:
        subprocess.run(
            cmd, cwd=str(contents_dir),
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        # ref might be a remote branch — try origin/{ref}.
        cmd2 = ["git", "worktree", "add", str(worktree_dir), f"origin/{ref}"]
        try:
            subprocess.run(
                cmd2, cwd=str(contents_dir),
                check=True, capture_output=True, text=True, timeout=60,
            )
        except subprocess.CalledProcessError as e2:
            raise RuntimeError(
                f"Failed to create worktree for {repo}@{ref}: {e2.stderr.strip()}"
            )

    return str(worktree_dir)


def _default_branch(bare_dir: Path) -> str:
    """Read the default branch from a bare repo's HEAD."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(bare_dir),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _is_tag(bare_dir: Path, ref: str) -> bool:
    """Check if *ref* is a tag in the bare repo."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--tags", ref],
            cwd=str(bare_dir),
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _maybe_fetch_branch(
    bare_dir: Path, worktree_dir: Path, ref: str, ttl_hours: int = 24, force: bool = False
) -> None:
    """Fetch the latest commits for a branch if older than *ttl_hours*."""
    last_fetch_file = worktree_dir / ".last_fetched"
    now = time.time()

    if not force and last_fetch_file.exists():
        try:
            mtime = last_fetch_file.stat().st_mtime
            if (now - mtime) < (ttl_hours * 3600):
                return
        except Exception:
            pass

    try:
        # 1. Fetch from origin into the bare repo
        subprocess.run(
            ["git", "fetch", "origin", f"{ref}:{ref}"],
            cwd=str(bare_dir),
            check=True, capture_output=True, text=True, timeout=60,
        )
        # 2. Reset the worktree to the new commit
        subprocess.run(
            ["git", "reset", "--hard", ref],
            cwd=str(worktree_dir),
            check=True, capture_output=True, text=True, timeout=30,
        )
        # 3. Update timestamp
        last_fetch_file.touch()
    except Exception:
        # Best effort - if network is down or merge fails, just keep going
        pass


# ---------------------------------------------------------------------------
# Serialization helpers (for CLI / MCP JSON output)
# ---------------------------------------------------------------------------


def mapping_to_dict(m: IdentifierMapping) -> dict[str, Any]:
    return asdict(m)


def module_mapping_to_dict(m: ModuleMapping) -> dict[str, Any]:
    return asdict(m)
