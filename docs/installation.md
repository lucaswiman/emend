# Installation

## From source

Clone the repository and install using the Makefile:

```bash
git clone <repo-url>
cd emend

# Create virtual environment and install with dev dependencies
make venv
```

Or manually:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Dependencies

emend uses LibCST as its sole AST backend -- all parsing, traversal, and transformation (including `list-symbols`, `lookup`, `find`, `rename`, and scope analysis) go through LibCST.

Runtime dependencies:

- [LibCST](https://github.com/Instagram/LibCST) -- Concrete syntax tree parsing and transformation
- [Typer](https://typer.tiangolo.com/) -- CLI framework
- [Lark](https://github.com/lark-parser/lark) -- Grammar-based parsing for selectors and patterns
- [PyYAML](https://pyyaml.org/) -- YAML parsing for batch operations and lint rules

## Verifying installation

```bash
emend --help
```
