"""A request-local source snapshot for composing edits before publication."""
from contextvars import ContextVar
import difflib
from pathlib import Path


_active: ContextVar["EditSession | None"] = ContextVar("edit_session", default=None)


def _generate_diff(file_path: str, old_code: str, new_code: str) -> str:
    return "".join(difflib.unified_diff(
        old_code.splitlines(keepends=True), new_code.splitlines(keepends=True),
        fromfile=file_path, tofile=file_path,
    ))


def current_edit_session():
    return _active.get()


def read_source(path: str | Path) -> str:
    session = _active.get()
    return session.read(path) if session else Path(path).read_text()


def write_source(path: str | Path, source: str, *, extension: str | None = None) -> None:
    session = _active.get()
    if session:
        path = Path(path).resolve()
        session.read(path)
        session.staged[path] = (source, extension or path.suffix.lstrip("."))
    else:
        Path(path).write_text(source)


class EditSession:
    """Compose existing-file edits; publish only after every operation succeeds."""

    def __init__(self):
        self.original: dict[Path, str] = {}
        self.staged: dict[Path, tuple[str, str]] = {}

    def __enter__(self):
        if _active.get() is not None:
            raise RuntimeError("Cannot nest edit sessions")
        self._token = _active.set(self)
        return self

    def __exit__(self, *exc):
        _active.reset(self._token)

    def read(self, path: str | Path) -> str:
        path = Path(path).resolve()
        if path not in self.original:
            self.original[path] = path.read_text()
        return self.staged.get(path, (self.original[path], ""))[0]

    def publish(self, apply: bool) -> str:
        from emend import emend_core

        changed = {path: (source, ext) for path, (source, ext) in self.staged.items()
                   if source != self.original[path]}
        for path, (source, ext) in changed.items():
            if not emend_core.validate_syntax(source, ext, fragment=False):
                raise ValueError(f"Planned edit would produce invalid syntax: {path}")
        for path, original in self.original.items():
            if path.read_text() != original:
                raise ValueError(f"Source changed during batch planning: {path}")
        output = "".join(_generate_diff(str(path), self.original[path], source)
                         for path, (source, _) in changed.items())
        if apply:
            for path, (source, _) in changed.items():
                path.write_text(source)
        return output
