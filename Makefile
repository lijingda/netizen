PYTHON ?= .venv/bin/python

.PHONY: check test

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q netizen scripts tests
	$(PYTHON) -m pip check
	$(PYTHON) scripts/probe_sdk_task_diff.py --timeout 5
	$(PYTHON) scripts/probe_sdk_turn_plan.py --timeout 5
	$(PYTHON) scripts/probe_sdk_completion_race.py --read-recovery --attempts 20 --timeout 3
	$(PYTHON) scripts/probe_sdk_completion_race.py --usage-drain --attempts 40 --timeout 10
