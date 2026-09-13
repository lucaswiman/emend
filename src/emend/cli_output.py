import json
from functools import partial
from pathlib import Path


def format_json(data: object) -> str:
    return json.dumps(data, indent=2)


def emit_json(data: object) -> None:
    print(format_json(data))


_ANSI_RESET = "\033[0m"
_ANSI_MAGENTA = "\033[35m"
_ANSI_GREEN = "\033[32m"
_ANSI_CYAN = "\033[36m"
_ANSI_RED_BOLD = "\033[1;31m"


def _print_pattern_match_code(
    file_path_str: str,
    match,
    file_lines_cache: dict[str, list[str]],
    *,
    is_tty: bool = False,
    stream=None,
) -> None:
    """Print a pattern match with a file:line header followed by matched source lines."""
    _print = partial(print, file=stream)
    if match.line is None:
        if is_tty:
            _print(
                f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}?{_ANSI_RESET}",
                flush=True,
            )
        else:
            _print(f"{file_path_str}:?", flush=True)
        return

    start_line = match.line
    end_line = match.end_line or start_line

    if file_path_str not in file_lines_cache:
        try:
            file_lines_cache[file_path_str] = Path(file_path_str).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            file_lines_cache[file_path_str] = []
    lines = file_lines_cache[file_path_str]

    line_range = str(start_line) if start_line == end_line else f"{start_line}-{end_line}"
    if is_tty:
        _print(
            f"{_ANSI_MAGENTA}{file_path_str}{_ANSI_CYAN}:{_ANSI_GREEN}{line_range}{_ANSI_RESET}",
            flush=True,
        )
    else:
        _print(f"{file_path_str}:{line_range}", flush=True)

    col = match.col
    end_col_val = match.end_col
    for i in range(start_line, min(end_line + 1, len(lines) + 1)):
        line_text = lines[i - 1] if i <= len(lines) else ""
        if is_tty and col is not None and end_col_val is not None:
            if start_line == end_line:
                hl_start = col
                hl_end = end_col_val
            elif i == start_line:
                hl_start = col
                hl_end = len(line_text)
            elif i == end_line:
                hl_start = 0
                hl_end = end_col_val
            else:
                hl_start = 0
                hl_end = len(line_text)
            hl_start = max(0, min(hl_start, len(line_text)))
            hl_end = max(hl_start, min(hl_end, len(line_text)))
            before = line_text[:hl_start]
            highlighted = line_text[hl_start:hl_end]
            after = line_text[hl_end:]
            _print(f"{before}{_ANSI_RED_BOLD}{highlighted}{_ANSI_RESET}{after}", flush=True)
        else:
            _print(line_text, flush=True)


def print_pattern_matches(matches, output: str, *, dedent: bool = False, is_tty: bool = False, stream=None) -> int:
    """Render text results consistently for CLI and MCP search."""
    from emend.ast_utils import find_nested_definitions, find_symbol_by_line
    from textwrap import dedent as dedent_text

    _print = partial(print, file=stream)
    definitions: dict[str, list] = {}
    file_lines: dict[str, list[str]] = {}
    seen: set[str] = set()
    count = 0
    for file_path, match in matches:
        count += 1
        location = f"{file_path}:{match.line if match.line is not None else '?'}"
        if output == "selector" and match.line is not None:
            if file_path not in definitions:
                definitions[file_path] = find_nested_definitions(file_path)
            symbol = find_symbol_by_line(definitions[file_path], match.line)
            if symbol:
                location = f"{file_path}::{'.'.join(symbol.path)}"
                if location in seen:
                    continue
                seen.add(location)
        if output in ("selector", "location", "summary"):
            _print(location, flush=True)
        elif dedent:
            _print(location, flush=True)
            _print(dedent_text(match.matched_text or match.node_text or "").rstrip(), flush=True)
        else:
            _print_pattern_match_code(file_path, match, file_lines, is_tty=is_tty, stream=stream)
    return count
