VENV := .venv
PYTHON := $(VENV)/bin/python
TESTS ?=

.PHONY: venv test test-mcp deadcode docs docs-html benchmark clean

RUST_SOURCES := $(wildcard rust/src/*.rs)

# Use local caches for sandboxed environments
export CARGO_HOME := $(CURDIR)/.cargo-cache
export UV_CACHE_DIR := $(CURDIR)/.uv-cache
export VIRTUAL_ENV := $(CURDIR)/$(VENV)
export PATH := $(VIRTUAL_ENV)/bin:$(shell echo $$PATH)

$(VENV)/bin/activate: pyproject.toml rust/Cargo.toml
	uv venv $(VENV) --python 3.14t
	uv pip install --python $(VENV) maturin
	touch $(VENV)/bin/activate

# Rebuild Rust extension when source files change
$(VENV)/lib/emend_core: $(RUST_SOURCES) rust/Cargo.toml | $(VENV)/bin/activate
	$(VENV)/bin/maturin develop --extras dev
	@mkdir -p $(@D) && touch $@

test: $(VENV)/bin/activate $(VENV)/lib/emend_core
	cargo test --manifest-path rust/Cargo.toml
	$(VENV)/bin/pytest --tb=short -n 8 $(if $(TESTS),$(TESTS),tests/)

PYTHON_VERSION ?= 3.14t
MCP_VENV := .venv-mcp

$(MCP_VENV)/bin/activate: pyproject.toml rust/Cargo.toml
	uv venv $(MCP_VENV) --python $(PYTHON_VERSION)
	uv pip install --python $(MCP_VENV) maturin
	@touch $@

$(MCP_VENV)/lib/emend_core: $(RUST_SOURCES) rust/Cargo.toml | $(MCP_VENV)/bin/activate
	VIRTUAL_ENV=$(CURDIR)/$(MCP_VENV) PATH=$(CURDIR)/$(MCP_VENV)/bin:$$PATH \
		$(MCP_VENV)/bin/maturin develop --extras dev,mcp
	@mkdir -p $(@D) && touch $@

test-mcp: $(MCP_VENV)/lib/emend_core
	$(MCP_VENV)/bin/pytest --tb=short -k TestMCPTools tests/test_emend/test_knowledge.py

deadcode: $(VENV)/bin/activate $(VENV)/lib/emend_core
	$(VENV)/bin/emend deadcode src/emend/ --exclude-references-from tests/ --no-last-reference

docs: docs-html

docs-html: $(VENV)/bin/activate
	$(VENV)/bin/sphinx-build -b html docs docs/_build/html

BENCH_LABEL ?=

benchmark: $(VENV)/bin/activate
	@mkdir -p benchmark-results
	$(PYTHON) benchmarks/bench_django.py --json --save benchmark-results/$$(date +%Y%m%d-%H%M%S).json --label "$(BENCH_LABEL)" $(BENCH_ARGS)

clean:
	rm -rf $(VENV) $(MCP_VENV) build *.egg-info src/*.egg-info docs/_build
