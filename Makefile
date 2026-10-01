# LLM Code and Review Tools - Makefile
#
# Targets:
#   make install   - Install all tools (jira, gerrit-cli, ...)
#   make configure - Set up the credentials the tools need
#   make status    - Show which tools have credentials configured
#   make uninstall - Uninstall all tools
#   make hooks     - Point git at the tracked hooks (version bump)
#   make test      - Run the installer tests
#   make unit-test - Run every tool's offline test suite
#   make help      - Show this help
#

.PHONY: install configure status uninstall hooks test unit-test help

help:
	@echo "LLM Code and Review Tools"
	@echo ""
	@echo "Usage:"
	@echo "  make install    Install all tools (jira, gerrit-cli, ...)"
	@echo "  make configure  Set up the credentials the tools need"
	@echo "  make status     Show which tools have credentials configured"
	@echo "  make uninstall  Uninstall all tools"
	@echo "  make hooks      Point git at the tracked hooks"
	@echo "  make test       Run the installer tests"
	@echo "  make unit-test  Run every tool's offline test suite"
	@echo "  make help       Show this help"
	@echo ""

install:
	@./install.sh

configure:
	@./install.sh --configure

status:
	@./install.sh --status

uninstall:
	@./install.sh --uninstall

# Per clone, once: the pre-commit hook bumps each tool's patch
# version when that tool has staged changes.
hooks:
	@git config core.hooksPath .githooks
	@echo "git hooks: .githooks (pre-commit version bump)"

test:
	@./test_install.sh

# One pytest process per tool: every tool keeps its tests in a package
# named "tests", and a single process imports them under the same names.
TEST_DIRS = llm_tool_common jira_tool gerrit_cli maloo_tool jenkins_tool \
	janitor_tool lreview lustre_crash patch_shepherd gerrit_dashboard
PYTHON ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,python3)

unit-test:
	@status=0; for d in $(TEST_DIRS); do \
		echo "== $$d"; \
		(cd $$d && $(PYTHON) -m pytest -q -m 'not integration') || status=1; \
	done; exit $$status
