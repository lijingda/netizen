PYTHON ?= .venv/bin/python

.PHONY: check test

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q netizen_cli scripts tests
	$(PYTHON) -m pip check
	$(PYTHON) scripts/check_sdk.py
