# Patch Watcher developer tasks.
#
# The unit suite is hermetic (no network, credentials, email, or browser).
# `integration` and `browser` are opt-in and need PATCH_WATCHER_TEST_CONFIG.

PYTHON ?= python3

# The browser check needs playwright, which normally lives in the tools venv
# rather than the system interpreter. That venv is not guaranteed to exist --
# install.sh only creates <repo>/.venv on PEP 668 hosts or with --venv, and
# --venv PATH puts it somewhere else entirely -- so fall back to $(PYTHON)
# instead of naming a path that is simply absent on most hosts.
TOOLS_VENV_PYTHON := $(HOME)/llm_code_and_review_tools/.venv/bin/python
VENV_PYTHON ?= $(if $(wildcard $(TOOLS_VENV_PYTHON)),$(TOOLS_VENV_PYTHON),$(PYTHON))

.PHONY: help check lint lint-fix test coverage integration browser doctor

help:
	@echo "make check        lint + unit tests (what CI should run)"
	@echo "make lint         ruff, no changes"
	@echo "make lint-fix     ruff with safe autofixes applied"
	@echo "make test         unit suite"
	@echo "make coverage     unit suite + line coverage report"
	@echo "make integration  opt-in: real Gerrit, read-only"
	@echo "make browser      opt-in: real Gerrit in Chrome, read-only"
	@echo "make doctor       can this host run agents?"

check: lint test

lint:
	ruff check .

lint-fix:
	ruff check . --fix

test:
	$(PYTHON) -m unittest discover -s . 

coverage:
	$(PYTHON) -m coverage run --source=patch_watcher -m unittest discover -s . -p 'test_*.py'
	$(PYTHON) -m coverage report --sort=miss

integration:
	@test -n "$$PATCH_WATCHER_TEST_CONFIG" || \
		{ echo "set PATCH_WATCHER_TEST_CONFIG to a patch-watcher config file"; exit 2; }
	$(PYTHON) integration_check.py

browser:
	@test -n "$$PATCH_WATCHER_TEST_CONFIG" || \
		{ echo "set PATCH_WATCHER_TEST_CONFIG to a patch-watcher config file"; exit 2; }
	$(VENV_PYTHON) browser_check.py

doctor:
	$(PYTHON) -m patch_watcher.pw_doctor
