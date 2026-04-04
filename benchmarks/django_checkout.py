"""Django checkout management for emend benchmarks.

Provides helpers to clone, cache, and set up a Django codebase for use in
benchmark scripts.  Extracted from bench_django.py so other scripts can reuse
the checkout without duplicating the git logic.

Public API
----------
ensure_django_checkout() -> Path
    Clone or verify the pinned Django checkout; returns its path.

ensure_scaled_checkout(django_dir: Path) -> Path
    Build a 50x-duplicated copy of the Django source for throughput tests.

setup_lint_rules(django_dir: Path) -> None
    Write a minimal .emend/patterns.yaml into the checkout for lint benchmarks.
"""
from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_utils import _log, _run  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DJANGO_REPO = "https://github.com/django/django.git"
DJANGO_COMMIT = "9e7cc2b628fe8fd3895986af9b7fc9525034c1b0"
DJANGO_TAG = "5.2"  # Tag that resolves to DJANGO_COMMIT (annotated tag)

_BENCHMARKS_DIR = Path(__file__).resolve().parent
CACHE_DIR = _BENCHMARKS_DIR / ".django-checkout"
SCALED_DIR = _BENCHMARKS_DIR / ".django-scaled"
SCALED_COPIES = 50  # Number of copies of django/ to create

LINT_RULES_YAML = textwrap.dedent("""\
    macros:
      print_call: "print($...ARGS)"
      isinstance_str: "isinstance($X, str)"

    rules:
      no-print:
        find: "{print_call}"
        message: "Avoid bare print() calls in production code."

      isinstance-str:
        find: "{isinstance_str}"
        message: "Consider using type($X) is str or more specific checks."

      no-hasattr:
        find: "hasattr($X, $Y)"
        message: "hasattr() swallows exceptions; use try/except or check __dict__."

      no-open-without-encoding:
        find: "open($PATH)"
        message: "Specify encoding when calling open()."

      no-mutable-default:
        find: "def $F($...A, $P=[], $...B):"
        message: "Mutable default argument; use None and initialize inside."
""")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def ensure_django_checkout() -> Path:
    """Clone Django into the cache directory if not already present.

    Uses a shallow clone at the exact commit (via its tag) to minimise
    download size.  Falls back to a full clone if needed.

    Returns the path to the Django checkout.
    """
    if CACHE_DIR.exists() and (CACHE_DIR / ".git").is_dir():
        # Verify we have the right commit checked out.
        result = _run(["git", "rev-parse", "HEAD"], cwd=CACHE_DIR)
        if result.returncode == 0 and result.stdout.strip() == DJANGO_COMMIT:
            _log(f"  Django checkout already present at {CACHE_DIR}")
            return CACHE_DIR
        else:
            _log("  Django checkout exists but wrong commit, resetting...")
            _run(
                ["git", "fetch", "--depth", "1", "origin", f"tag {DJANGO_TAG}"],
                cwd=CACHE_DIR,
            )
            result = _run(
                ["git", "checkout", f"tags/{DJANGO_TAG}"], cwd=CACHE_DIR
            )
            if result.returncode != 0:
                _log(f"  Failed to checkout tag {DJANGO_TAG}, re-cloning...")
                shutil.rmtree(CACHE_DIR)

    if not CACHE_DIR.exists():
        _log(f"  Cloning Django (tag {DJANGO_TAG}) into {CACHE_DIR}...")
        result = _run([
            "git", "clone",
            "--branch", DJANGO_TAG,
            "--depth", "1",
            DJANGO_REPO,
            str(CACHE_DIR),
        ])
        if result.returncode != 0:
            print(
                f"ERROR: Failed to clone Django at tag {DJANGO_TAG}:\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            sys.exit(1)

        # Verify the commit matches what we expect.
        result = _run(["git", "rev-parse", "HEAD"], cwd=CACHE_DIR)
        actual_commit = (
            result.stdout.strip() if result.returncode == 0 else "<unknown>"
        )
        if actual_commit != DJANGO_COMMIT:
            print(
                f"  WARNING: Tag {DJANGO_TAG} resolved to {actual_commit}, "
                f"expected {DJANGO_COMMIT}. Proceeding anyway.",
                file=sys.stderr,
            )

    _log(f"  Django checkout ready at {CACHE_DIR}")
    return CACHE_DIR


def ensure_scaled_checkout(django_dir: Path) -> Path:
    """Create a scaled directory with N copies of django/.

    The result is a directory like::

        .django-scaled/
            django1/django/...
            django2/django/...
            ...
            django50/django/...

    Uses hard links for .py files to avoid duplicating data while keeping the
    directory structure real (no symlinks, so scanners work natively).

    Returns the path to the scaled directory.
    """
    marker = SCALED_DIR / f".{SCALED_COPIES}-copies"
    if SCALED_DIR.exists() and marker.exists():
        _log(
            f"  Scaled checkout already present at {SCALED_DIR} "
            f"({SCALED_COPIES} copies)"
        )
        return SCALED_DIR

    if SCALED_DIR.exists():
        _log("  Scaled checkout exists but stale, recreating...")
        shutil.rmtree(SCALED_DIR)

    _log(f"  Creating scaled checkout with {SCALED_COPIES} copies...")
    SCALED_DIR.mkdir(parents=True)
    src_django = django_dir / "django"

    py_files = list(src_django.rglob("*.py"))
    rel_files = [p.relative_to(src_django) for p in py_files]

    _log(f"  Source: {len(rel_files)} .py files in {src_django}")

    for i in range(1, SCALED_COPIES + 1):
        dest_root = SCALED_DIR / f"django{i}" / "django"
        for rel in rel_files:
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = src_django / rel
            try:
                dest.hardlink_to(src)
            except OSError:
                shutil.copy2(str(src), str(dest))

    total_py = len(rel_files) * SCALED_COPIES
    _log(f"  Scaled checkout ready: {total_py} .py files in {SCALED_DIR}")
    marker.write_text(f"{SCALED_COPIES}\n")
    return SCALED_DIR


def setup_lint_rules(django_dir: Path) -> None:
    """Create .emend/patterns.yaml in the Django checkout for lint benchmarks."""
    emend_dir = django_dir / ".emend"
    emend_dir.mkdir(exist_ok=True)
    rules_file = emend_dir / "patterns.yaml"
    rules_file.write_text(LINT_RULES_YAML)
    _log(f"  Lint rules written to {rules_file}")
