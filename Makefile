VENV := .venv
PYTHON := $(VENV)/bin/python
TESTS ?=

.PHONY: venv test docs docs-html benchmark clean

RUST_SOURCES := $(wildcard rust/src/*.rs)

$(VENV)/bin/activate: pyproject.toml rust/Cargo.toml rust/pyproject.toml
	python3 -m venv $(VENV) --without-pip
	curl https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
	$(VENV)/bin/python3 /tmp/get-pip.py
	$(VENV)/bin/pip install maturin
	$(VENV)/bin/maturin develop --manifest-path rust/Cargo.toml
	$(VENV)/bin/pip install -e ".[dev]"
	touch $(VENV)/bin/activate

# Rebuild Rust extension when source files change
$(VENV)/lib/emend_core: $(RUST_SOURCES) rust/Cargo.toml | $(VENV)/bin/activate
	$(VENV)/bin/pip install -q maturin
	$(VENV)/bin/maturin develop --manifest-path rust/Cargo.toml
	@mkdir -p $(@D) && touch $@

test: $(VENV)/bin/activate $(VENV)/lib/emend_core
	$(VENV)/bin/pytest --tb=short -n 8 $(if $(TESTS),$(TESTS),tests/)

docs: docs-html

docs-html: $(VENV)/bin/activate
	$(VENV)/bin/sphinx-build -b html docs docs/_build/html

benchmark: $(VENV)/bin/activate
	$(PYTHON) benchmarks/bench_django.py $(BENCH_ARGS)

clean:
	rm -rf $(VENV) build *.egg-info src/*.egg-info docs/_build
