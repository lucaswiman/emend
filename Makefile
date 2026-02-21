VENV := .venv
PYTHON := $(VENV)/bin/python
TESTS ?=

.PHONY: venv test docs clean

$(VENV)/bin/activate: pyproject.toml
	python3 -m venv $(VENV) --without-pip
	curl https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
	$(VENV)/bin/python3 /tmp/get-pip.py
	$(VENV)/bin/pip install -e ".[dev]"
	touch $(VENV)/bin/activate

test: $(VENV)/bin/activate
	$(VENV)/bin/pytest --tb=short -n 8 $(if $(TESTS),$(TESTS),tests/)

docs: $(VENV)/bin/activate
	$(VENV)/bin/sphinx-build -b html docs docs/_build

clean:
	rm -rf $(VENV) build *.egg-info src/*.egg-info
