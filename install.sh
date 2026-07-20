#!/bin/bash
#
# Unified installer for LLM Code and Review Tools
# Installs: jira, gerrit-cli, maloo, jenkins, lustre-crash, janitor, lreview, and beads (bd)
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
    echo "Install LLM code and review tools (jira, gerrit-cli, maloo, jenkins, lustre-crash, janitor, lreview, beads)"
    echo ""
    echo "Options:"
    echo "  --help, -h     Show this help message"
    echo "  --uninstall    Uninstall all tools"
    echo "  --venv [PATH]  Install into a virtual environment (created if"
    echo "                 missing; default path: <repo>/.venv). Offered"
    echo "                 automatically when the system Python is"
    echo "                 externally managed (PEP 668, e.g. Homebrew)."
    echo ""
}

check_python() {
    for py in python3.12 python3.11 python3; do
        if command -v $py &> /dev/null; then
            version=$($py -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            major=$(echo $version | cut -d. -f1)
            minor=$(echo $version | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                echo $py
                return 0
            fi
        fi
    done
    return 1
}

# True when pip installs with this interpreter would be refused by
# PEP 668 (marker file in the stdlib dir, and not already in a venv) —
# the "externally-managed-environment" error from Homebrew/Debian
# pythons.
is_externally_managed() {
    "$1" -c '
import os, sys, sysconfig
in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
sys.exit(0 if (not in_venv and os.path.exists(marker)) else 1)'
}

# Resolve the interpreter to install with. Sets PYTHON and VENV_USED.
# An existing venv (default <repo>/.venv, or --venv PATH) is reused;
# otherwise, if the system Python is externally managed (or --venv was
# given), offer to create a venv — ssh-keygen style, with an editable
# path — and install into it.
resolve_python() {
    local base_py="$1"
    VENV_USED=""
    local venv="${VENV_PATH:-$SCRIPT_DIR/.venv}"

    if [ -x "$venv/bin/python" ]; then
        PYTHON="$venv/bin/python"
        VENV_USED="$venv"
        echo -e "${GREEN}✓${NC} Using existing virtual environment: $venv"
        return 0
    fi

    if [ "$VENV_FLAG" -eq 0 ] && ! is_externally_managed "$base_py"; then
        PYTHON="$base_py"
        return 0
    fi

    if [ "$VENV_FLAG" -eq 0 ]; then
        echo ""
        echo -e "${YELLOW}This Python is externally managed (PEP 668):${NC} pip refuses to"
        echo "install packages outside a virtual environment (typical for"
        echo "Homebrew Python on macOS and system Python on newer distros)."
        if [ ! -t 0 ]; then
            echo "Re-run interactively, or choose a venv up front:"
            echo "  ./install.sh --venv          # uses $venv"
            echo "  ./install.sh --venv PATH"
            return 1
        fi
        local answer
        read -r -p "Create a virtual environment for the tools? [Y/n] " answer || true
        case "$answer" in
            n|N|no|NO)
                echo "Aborted. To install manually into a venv of your choice:"
                echo "  $base_py -m venv $venv"
                echo "  $venv/bin/pip install -e <tool_dir>"
                return 1
                ;;
        esac
    fi

    if [ -z "$VENV_PATH" ] && [ -t 0 ]; then
        local answer
        read -r -p "Enter venv path [$venv]: " answer || true
        venv="${answer:-$venv}"
    fi

    echo "Creating virtual environment: $venv"
    "$base_py" -m venv "$venv" || {
        echo -e "${RED}Failed to create virtual environment at $venv${NC}"
        return 1
    }
    PYTHON="$venv/bin/python"
    VENV_USED="$venv"
    "$PYTHON" -m pip install -q --upgrade pip 2>/dev/null || true
}

install_tools() {
    echo "========================================"
    echo "LLM Code and Review Tools - Installer"
    echo "========================================"
    echo ""

    # Check Python
    PYTHON=$(check_python) || {
        echo -e "${RED}Error: Python 3.11+ required${NC}"
        exit 1
    }
    resolve_python "$PYTHON" || exit 1
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

    # Install gerrit_cli
    echo ""
    echo "Installing gerrit-cli..."
    $PYTHON -m pip uninstall -y gerrit-comments 2>/dev/null || true
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/gerrit_cli"
    echo -e "${GREEN}✓${NC} gerrit-cli installed"

    # Install maloo_tool
    echo ""
    echo "Installing maloo..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/maloo_tool"
    echo -e "${GREEN}✓${NC} maloo installed"

    # Install jenkins_tool
    echo ""
    echo "Installing jenkins..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/jenkins_tool"
    echo -e "${GREEN}✓${NC} jenkins installed"

    # Initialize submodules
    echo ""
    echo "Initializing submodules..."
    (cd "$SCRIPT_DIR" && git submodule update --init --recursive 2>/dev/null || true)
    echo -e "${GREEN}✓${NC} submodules initialized"

    # Install lustre_crash
    echo ""
    echo "Installing lustre-crash..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/lustre_crash"
    echo -e "${GREEN}✓${NC} lustre-crash installed"

    # Install janitor_tool
    echo ""
    echo "Installing janitor..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/janitor_tool"
    echo -e "${GREEN}✓${NC} janitor installed"

    # Install lreview
    echo ""
    echo "Installing lreview..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/lreview"
    echo -e "${GREEN}✓${NC} lreview installed"

    # Install drgn + lustre-drgn-tools
    if [[ -d "$SCRIPT_DIR/lustre-drgn-tools" ]]; then
        echo ""
        echo "Installing drgn and lustre-drgn-tools..."
        if $PYTHON -c "import drgn" 2>/dev/null; then
            echo -e "${GREEN}✓${NC} drgn already installed"
        else
            echo "  Installing drgn..."
            if [[ -x "$SCRIPT_DIR/lustre-drgn-tools/install-drgn.sh" ]] \
                && "$SCRIPT_DIR/lustre-drgn-tools/install-drgn.sh"; then
                echo -e "${GREEN}✓${NC} drgn installed"
            elif $PYTHON -m pip install -q drgn; then
                echo -e "${GREEN}✓${NC} drgn installed via pip"
            else
                echo -e "${YELLOW}warning:${NC} drgn install failed" \
                    "(optional; only needed for lustre-drgn-tools)"
            fi
        fi
        echo -e "${GREEN}✓${NC} lustre-drgn-tools ready"
    fi

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
    if [ -n "$VENV_USED" ]; then
        echo "Tools are installed in a virtual environment:"
        echo "  $VENV_USED"
        echo "Activate it, or add it to PATH:"
        echo "  source $VENV_USED/bin/activate"
        echo "  export PATH=\"$VENV_USED/bin:\$PATH\""
        echo ""
    fi
    echo "Installed tools:"
    echo "  jira            - JIRA issue tracking"
    echo "  gerrit          - Gerrit code review (also: gc)"
    echo "  maloo           - Maloo test results"
    echo "  jenkins         - Jenkins build server"
    echo "  lustre-crash    - Non-interactive crash dump analysis"
    echo "  janitor         - Gerrit Janitor test results"
    echo "  lreview         - Parallel AI patch reviews (kreview)"
    echo "  bd              - Beads task tracking"
    echo ""
    echo "Verify installation:"
    echo "  jira --help"
    echo "  gerrit --help"
    echo "  maloo --help"
    echo "  jenkins --help"
    echo "  lustre-crash --help"
    echo "  janitor --help"
    echo "  lreview check"
    echo "  bd --help"
    echo ""
    echo "Configuration:"
    echo "  JIRA:    Set JIRA_SERVER and JIRA_TOKEN env vars"
    echo "  Gerrit:  Set GERRIT_URL, GERRIT_USER, GERRIT_PASS env vars (config dir: ~/.config/gerrit-cli)"
    echo "  Maloo:   Set MALOO_USER and MALOO_PASS env vars"
    echo "  Jenkins: Set JENKINS_URL, JENKINS_USER, JENKINS_TOKEN env vars"
    echo "  Beads:   Run 'bd init --stealth' in your project"
    echo ""
    echo "See AGENTS.md for usage documentation."
}

uninstall_tools() {
    echo "========================================"
    echo "LLM Code and Review Tools - Uninstaller"
    echo "========================================"
    echo ""

    PYTHON=$(check_python) || {
        echo -e "${RED}Error: Python 3.11+ required${NC}"
        exit 1
    }

    # Uninstall from the venv the tools were installed into, if any
    local venv="${VENV_PATH:-$SCRIPT_DIR/.venv}"
    if [ -x "$venv/bin/python" ]; then
        PYTHON="$venv/bin/python"
        echo "Using virtual environment: $venv"
    fi

    echo "Uninstalling jira-tool..."
    $PYTHON -m pip uninstall -y jira-tool 2>/dev/null || true

    echo "Uninstalling gerrit-cli..."
    $PYTHON -m pip uninstall -y gerrit-cli 2>/dev/null || true
    $PYTHON -m pip uninstall -y gerrit-comments 2>/dev/null || true

    echo "Uninstalling maloo-tool..."
    $PYTHON -m pip uninstall -y maloo-tool 2>/dev/null || true

    echo "Uninstalling jenkins-tool..."
    $PYTHON -m pip uninstall -y jenkins-tool 2>/dev/null || true

    echo "Uninstalling janitor-tool..."
    $PYTHON -m pip uninstall -y janitor-tool 2>/dev/null || true

    echo "Uninstalling lreview..."
    $PYTHON -m pip uninstall -y lreview 2>/dev/null || true

    echo "Uninstalling lustre-crash..."
    $PYTHON -m pip uninstall -y lustre-crash 2>/dev/null || true
    $PYTHON -m pip uninstall -y crash-tool 2>/dev/null || true

    echo "Uninstalling llm-tool-common..."
    $PYTHON -m pip uninstall -y llm-tool-common 2>/dev/null || true

    echo ""
    echo -e "${GREEN}✓${NC} Python tools uninstalled"
    echo ""
    echo -e "${YELLOW}Note:${NC} beads (bd) not uninstalled - remove manually if needed:"
    echo "  rm ~/.local/bin/bd"
    echo ""
}

# Allow sourcing the functions without running the installer (tests)
if [ -n "${INSTALL_SH_NO_MAIN:-}" ]; then return 0 2>/dev/null || exit 0; fi

# Parse arguments
ACTION="install"
VENV_FLAG=0
VENV_PATH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --uninstall)
            ACTION="uninstall"
            ;;
        --venv)
            VENV_FLAG=1
            if [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then
                VENV_PATH="$2"
                shift
            fi
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            exit 1
            ;;
    esac
    shift
done

if [ "$ACTION" = "uninstall" ]; then
    uninstall_tools
else
    install_tools
fi
