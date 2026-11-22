PYTHON ?= .venv/bin/python

.PHONY: check check-browser check-native test lint typecheck

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

check-browser:
	NETIZEN_BROWSER_SMOKE=1 $(PYTHON) -m unittest tests.admin.test_browser_smoke

check-native:
	@evidence_dir="$${NATIVE_EVIDENCE_DIR:-$$(mktemp -d)}" && \
		echo "Native regression evidence: $$evidence_dir" && \
		$(PYTHON) scripts/run_upgrade_regression.py --group isolated --output-dir "$$evidence_dir"

.PHONY: check-native-claude
check-native-claude:
	env -i PATH=/usr/bin:/bin LANG=C.UTF-8 $(PYTHON) -I -B scripts/check_native_claude.py
