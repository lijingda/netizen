PYTHON ?= .venv/bin/python

.PHONY: check test lint typecheck

# Buffer passing tests while preserving output from failed tests.
test:
	$(PYTHON) -m unittest discover -s tests -v -b

# Correctness lint (pyflakes and dangling asyncio tasks); configured in pyproject.toml.
lint:
	$(PYTHON) -m ruff check netizen_cli scripts tests _build.py

# Type check the package, including the one-way layering of CodexRuntime and
# ChannelApplication; the per-module typing debt list lives in pyproject.toml.
typecheck:
	$(PYTHON) -m mypy

check: lint typecheck test
	$(PYTHON) -m compileall -q netizen_cli scripts tests
	$(PYTHON) -m pip check
	$(PYTHON) scripts/check_sdk.py
