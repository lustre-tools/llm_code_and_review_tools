# LLM Code and Review Tools - Makefile
#
# Targets:
#   make install   - Install all tools (jira, gerrit-cli, ...)
#   make configure - Set up the credentials the tools need
#   make status    - Show which tools have credentials configured
#   make uninstall - Uninstall all tools
#   make test      - Run the installer tests
#   make help      - Show this help
#

.PHONY: install configure status uninstall test help

help:
	@echo "LLM Code and Review Tools"
	@echo ""
	@echo "Usage:"
	@echo "  make install    Install all tools (jira, gerrit-cli, ...)"
	@echo "  make configure  Set up the credentials the tools need"
	@echo "  make status     Show which tools have credentials configured"
	@echo "  make uninstall  Uninstall all tools"
	@echo "  make test       Run the installer tests"
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

test:
	@./test_install.sh
