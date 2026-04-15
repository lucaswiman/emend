"""Corpus fetching layer for the AST dedup experiment.

Provides a small registry of Python codebases we want to canonicalize and
hash, along with helpers to clone / cache them on disk and iterate their
``.py`` files in deterministic order.

Public API
----------
CorpusSpec
    Frozen dataclass describing a single corpus.
CORPORA
    Mapping of ``name -> CorpusSpec`` for every known corpus.
CORPORA_CACHE
    Directory under ``experiments/ast_dedup`` where git-backed corpora are
    cached.  Created lazily by :func:`ensure`.
ensure(name)
    Make sure the named corpus is available on disk; returns its source root.
iter_py_files(corpus_root, max_files=None)
    Deterministically yield ``.py`` files under ``corpus_root``.
resolve_repo_root()
    Return the emend repository root (the parent of ``experiments/``).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

__all__ = [
    "CorpusSpec",
    "CORPORA",
    "CORPORA_CACHE",
    "ensure",
    "iter_py_files",
    "resolve_repo_root",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusSpec:
    name: str
    url: Optional[str]
    commit: Optional[str]
    tag: Optional[str]
    subpath: str
    description: str = ""


# ---------------------------------------------------------------------------
# Paths / registry
# ---------------------------------------------------------------------------


CORPORA_CACHE: Path = Path(__file__).resolve().parent / ".corpora"


CORPORA: dict[str, CorpusSpec] = {
    "emend": CorpusSpec(
        name="emend",
        url=None,
        commit=None,
        tag=None,
        subpath="src/emend",
        description="emend's own source",
    ),
    "django": CorpusSpec(
        name="django",
        url="https://github.com/django/django.git",
        commit="9e7cc2b628fe8fd3895986af9b7fc9525034c1b0",
        tag="5.2",
        subpath="django",
        description="Django 5.2",
    ),
    "cpython": CorpusSpec(
        name="cpython",
        url="https://github.com/python/cpython.git",
        commit=None,
        tag="v3.12.7",
        subpath="Lib",
        description="CPython 3.12.7 stdlib",
    ),
    "flask": CorpusSpec(
        name="flask",
        url="https://github.com/pallets/flask.git",
        commit=None,
        tag="3.0.3",
        subpath="src/flask",
        description="Flask 3.0.3",
    ),
    "pandas": CorpusSpec(
        name="pandas",
        url="https://github.com/pandas-dev/pandas.git",
        commit=None,
        tag="v2.2.3",
        subpath="pandas/core",
        description="pandas 2.2.3 core",
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[corpora] {msg}", file=sys.stderr)


def _run(
    cmd: list[str], *, cwd: Optional[Path] = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_repo_root() -> Path:
    """Return the emend repo root (the parent of ``experiments/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "experiments":
            return parent.parent
    # Fallback: two-up from this file (experiments/ast_dedup/corpora.py).
    return here.parent.parent.parent


def _git_rev_parse(cache_dir: Path, ref: str) -> Optional[str]:
    result = _run(["git", "rev-parse", ref], cwd=cache_dir)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _verify_checkout(spec: CorpusSpec, cache_dir: Path) -> bool:
    """Return True if the existing cache dir already matches the pinned ref."""
    head = _git_rev_parse(cache_dir, "HEAD")
    if head is None:
        return False
    if spec.commit:
        return head == spec.commit
    if spec.tag:
        # Dereference annotated tags to a commit SHA, then compare.
        tag_sha = _git_rev_parse(cache_dir, f"tags/{spec.tag}^{{}}")
        if tag_sha is None:
            # Lightweight tag: compare against the tag ref directly.
            tag_sha = _git_rev_parse(cache_dir, f"tags/{spec.tag}")
        if tag_sha is None:
            return False
        return head == tag_sha
    return True


def _clone(spec: CorpusSpec, cache_dir: Path) -> None:
    assert spec.url is not None and spec.tag is not None
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    _log(f"cloning {spec.name} (tag {spec.tag}) into {cache_dir}...")
    result = _run(
        [
            "git",
            "clone",
            "--branch",
            spec.tag,
            "--depth",
            "1",
            spec.url,
            str(cache_dir),
        ]
    )
    if result.returncode != 0:
        shutil.rmtree(cache_dir, ignore_errors=True)
        raise RuntimeError(
            f"failed to clone {spec.name} from {spec.url} at tag "
            f"{spec.tag}:\n{result.stderr.strip()}"
        )
    if spec.commit:
        head = _git_rev_parse(cache_dir, "HEAD")
        if head != spec.commit:
            _log(
                f"WARNING: {spec.name} tag {spec.tag} resolved to {head}, "
                f"expected {spec.commit}."
            )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def ensure(name: str) -> Path:
    """Ensure the named corpus is available on disk and return its source root.

    For the ``emend`` corpus this returns the local ``src/emend`` directory
    in the live repo and performs no git operations.  For git-backed corpora
    this shallow-clones into :data:`CORPORA_CACHE` if needed and verifies the
    pinned commit / tag.

    Raises
    ------
    KeyError
        If ``name`` is not in :data:`CORPORA`.
    RuntimeError
        If a clone, checkout, or verification step fails, or if the resulting
        ``subpath`` does not exist on disk.
    """
    if name not in CORPORA:
        raise KeyError(f"unknown corpus: {name!r} (known: {sorted(CORPORA)})")
    spec = CORPORA[name]

    if spec.url is None:
        # Local corpus: use the live repo.
        repo_root = resolve_repo_root()
        root = repo_root / spec.subpath
        if not root.is_dir():
            raise RuntimeError(
                f"local corpus {name!r}: {root} does not exist"
            )
        return root.resolve()

    CORPORA_CACHE.mkdir(parents=True, exist_ok=True)
    cache_dir = CORPORA_CACHE / name

    if cache_dir.exists() and (cache_dir / ".git").is_dir():
        if _verify_checkout(spec, cache_dir):
            _log(f"{name}: cache hit at {cache_dir}")
        else:
            _log(
                f"{name}: cache at {cache_dir} does not match pin; "
                f"removing and re-cloning"
            )
            shutil.rmtree(cache_dir)
            _clone(spec, cache_dir)
    else:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        _clone(spec, cache_dir)

    root = cache_dir / spec.subpath
    if not root.is_dir():
        raise RuntimeError(
            f"corpus {name!r}: expected subpath {spec.subpath!r} under "
            f"{cache_dir} but {root} does not exist"
        )
    return root.resolve()


def iter_py_files(
    corpus_root: Path, max_files: Optional[int] = None
) -> Iterator[Path]:
    """Yield absolute ``.py`` files under ``corpus_root`` in sorted order.

    Excludes ``__pycache__`` and ``.git`` directories and any path segment
    starting with ``.``.  Stops after ``max_files`` files if provided.
    """
    root = corpus_root.resolve()
    all_files = sorted(root.rglob("*.py"))

    count = 0
    for path in all_files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        skip = False
        for part in parts:
            if part in ("__pycache__", ".git"):
                skip = True
                break
            if part.startswith(".") and part not in (".", ".."):
                skip = True
                break
        if skip:
            continue
        yield path.resolve()
        count += 1
        if max_files is not None and count >= max_files:
            return
