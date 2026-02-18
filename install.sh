#!/bin/bash
#
# Unified installer for LLM Code and Review Tools
# Installs: jira, gerrit-comments, and beads (bd)
#

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Install LLM code and review tools (jira, gerrit-comments, beads)"
    echo ""
    echo "Options:"
    echo "  --help, -h     Show this help message"
    echo "  --uninstall    Uninstall all tools"
    echo ""
}

check_python() {
    for py in python3.11 python3.10 python3.9 python3; do
        if command -v $py &> /dev/null; then
            version=$($py -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            major=$(echo $version | cut -d. -f1)
            minor=$(echo $version | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
                echo $py
                return 0
            fi
        fi
    done
    return 1
}

install_tools() {
    echo "========================================"
    echo "LLM Code and Review Tools - Installer"
    echo "========================================"
    echo ""

    # Check Python
    PYTHON=$(check_python) || {
        echo -e "${RED}Error: Python 3.9+ required${NC}"
        exit 1
    }
    echo -e "${GREEN}✓${NC} Found Python: $PYTHON"

    # Install llm_tool_common first (shared dependency)
    echo ""
    echo "Installing llm-tool-common..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/llm_tool_common"
    echo -e "${GREEN}✓${NC} llm-tool-common installed"

    # Install jira_tool
    echo ""
    echo "Installing jira..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/jira_tool"
    echo -e "${GREEN}✓${NC} jira installed"

    # Install gerrit_comments
    echo ""
    echo "Installing gerrit-comments..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/gerrit_comments"
    echo -e "${GREEN}✓${NC} gerrit-comments installed"

    # Install maloo_tool
    echo ""
    echo "Installing maloo..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/maloo_tool"
    echo -e "${GREEN}✓${NC} maloo installed"

    # Install beads (bd)
    echo ""
    echo "Installing beads (bd)..."
    if command -v bd &> /dev/null; then
        echo -e "${GREEN}✓${NC} beads already installed: $(bd version 2>/dev/null | head -1)"
    else
        if command -v go &> /dev/null; then
            go install github.com/steveyegge/beads/cmd/bd@latest
            echo -e "${GREEN}✓${NC} beads installed via go"
        else
            curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash
            echo -e "${GREEN}✓${NC} beads installed via script"
        fi
    fi

    echo ""
    echo "========================================"
    echo -e "${GREEN}Installation Complete!${NC}"
    echo "========================================"
    echo ""
    echo "Installed tools:"
    echo "  jira            - JIRA issue tracking"
    echo "  gerrit-comments - Gerrit code review"
    echo "  maloo           - Maloo test results"
    echo "  bd              - Beads task tracking"
    echo ""
    echo "Verify installation:"
    echo "  jira --help"
    echo "  gerrit-comments --help"
    echo "  maloo --help"
    echo "  bd --help"
    echo ""
    echo "Configuration:"
    echo "  JIRA:   Set JIRA_SERVER and JIRA_TOKEN env vars"
    echo "  Gerrit: Set GERRIT_URL, GERRIT_USER, GERRIT_PASS env vars"
    echo "  Maloo:  Set MALOO_USER and MALOO_PASS env vars"
    echo "  Beads:  Run 'bd init --stealth' in your project"
    echo ""
    echo "See AGENTS.md for usage documentation."
}

uninstall_tools() {
    echo "========================================"
    echo "LLM Code and Review Tools - Uninstaller"
    echo "========================================"
    echo ""

    PYTHON=$(check_python) || {
        echo -e "${RED}Error: Python 3.9+ required${NC}"
        exit 1
    }

    echo "Uninstalling jira-tool..."
    $PYTHON -m pip uninstall -y jira-tool 2>/dev/null || true

    echo "Uninstalling gerrit-comments..."
    $PYTHON -m pip uninstall -y gerrit-comments 2>/dev/null || true

    echo "Uninstalling maloo-tool..."
    $PYTHON -m pip uninstall -y maloo-tool 2>/dev/null || true

    echo "Uninstalling llm-tool-common..."
    $PYTHON -m pip uninstall -y llm-tool-common 2>/dev/null || true

    echo ""
    echo -e "${GREEN}✓${NC} Python tools uninstalled"
    echo ""
    echo -e "${YELLOW}Note:${NC} beads (bd) not uninstalled - remove manually if needed:"
    echo "  rm ~/.local/bin/bd"
    echo ""
}

# Parse arguments
case "${1:-}" in
    --help|-h)
        usage
        exit 0
        ;;
    --uninstall)
        uninstall_tools
        exit 0
        ;;
    "")
        install_tools
        exit 0
        ;;
    *)
        echo -e "${RED}Unknown option: $1${NC}"
        usage
        exit 1
        ;;
esac
