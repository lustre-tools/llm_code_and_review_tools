#!/bin/bash
#
# Unified installer for LLM Code and Review Tools
# Installs: jira, gerrit-cli, maloo, jenkins, lustre-crash, janitor, lreview, and gerrit-dashboard
#
# Run it either way:
#   ./install.sh [OPTIONS]          # normal run
#   source install.sh [OPTIONS]     # same, then activates the venv
#                                   # in your current shell (zsh/bash)

# When sourced, run the installer as a child process and then activate
# the venv it used in the *current* shell — the one thing a child
# process cannot do itself. The early `return` also stops the shell
# from parsing the rest of this bash script.
_lct_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
    case "${ZSH_EVAL_CONTEXT:-}" in *:file*) _lct_sourced=1 ;; esac
elif [ -n "${BASH_VERSION:-}" ]; then
    [ "${BASH_SOURCE[0]}" != "$0" ] && _lct_sourced=1
fi
if [ "$_lct_sourced" = 1 ] && [ -z "${INSTALL_SH_NO_MAIN:-}" ]; then
    if [ -n "${ZSH_VERSION:-}" ]; then
        _lct_script="${(%):-%x}"
    else
        _lct_script="${BASH_SOURCE[0]}"
    fi
    _lct_state="$(mktemp)"
    INSTALL_SH_VENV_FILE="$_lct_state" bash "$_lct_script" "$@"
    _lct_rc=$?
    _lct_venv=""
    [ -r "$_lct_state" ] && _lct_venv="$(cat "$_lct_state")"
    rm -f "$_lct_state"
    if [ "$_lct_rc" -eq 0 ] && [ -n "$_lct_venv" ] \
        && [ -f "$_lct_venv/bin/activate" ]; then
        . "$_lct_venv/bin/activate"
        echo ""
        echo "venv activated in your current shell: $_lct_venv"
    fi
    unset _lct_sourced _lct_script _lct_state _lct_venv
    return $_lct_rc
fi
unset _lct_sourced

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Colors are for a terminal: a redirected install log should not be full of
# escape sequences.  NO_COLOR is honoured too (https://no-color.org).
if [ ! -t 1 ] || [ -n "${NO_COLOR:-}" ]; then
    GREEN=''
    RED=''
    YELLOW=''
    NC=''
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Console scripts our packages install (used for ~/.local/bin symlinks
# when installing into a venv, and for cleanup on uninstall)
TOOL_BINS="jira gerrit gerrit-cli gc maloo jenkins janitor lustre-crash lreview patch-watcher pw-doctor pw-configure"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "       source $0 [OPTIONS]   (activates the venv in your shell)"
    echo ""
    echo "Install LLM code and review tools (jira, gerrit-cli, maloo, jenkins, lustre-crash, janitor, lreview, gerrit-dashboard)"
    echo ""
    echo "Options:"
    echo "  --help, -h     Show this help message"
    echo "  --uninstall    Uninstall all tools"
    echo "  --configure    Walk through the credentials the tools need:"
    echo "                 what each one is for, where to get it, and"
    echo "                 \"not now\" as an answer. Writes only"
    echo "                 ~/.config/<tool>/.env, mode 0600."
    echo "  --only TOOL    Configure just one tool (gerrit, jira, maloo,"
    echo "                 jenkins); repeatable"
    echo "  --reconfigure  Prompt for values that are already set too"
    echo "  --no-verify    Do not check entered credentials against the"
    echo "                 server"
    echo "  --status       Show which tools have credentials configured"
    echo "  --no-configure Install without offering the credential walkthrough"
    echo "  --with-ltvm    Also install ltvm from lustre-test-vms-v2, which"
    echo "                 Patch Watcher agents need to create test VMs"
    echo "  --doctor       Check whether this host can run Patch Watcher agents"
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

# Pick the interpreter for --configure and --doctor. Sets PYTHON.
#
# Those two called resolve_python with the still-unset $PYTHON, so the PEP 668
# probe ran the empty string ("line 99: : command not found") and the tool was
# then exec'd as `"" -m patch_watcher.pw_configure`, exiting 127. They do not
# want resolve_python either: it exists to decide where pip may install, and
# neither of these installs anything -- patch_watcher is dependency-free and
# runs straight out of the checkout. Demanding a venv (which resolve_python
# does on any PEP 668 host, and refuses to do without a tty) would make both
# documented setup commands unusable there. So: reuse a venv if one is already
# there, otherwise just take a new-enough python3.
require_runtime_python() {
    local venv="${VENV_PATH:-$SCRIPT_DIR/.venv}"
    if [ -x "$venv/bin/python" ]; then
        PYTHON="$venv/bin/python"
        return 0
    fi
    PYTHON=$(check_python) || {
        echo -e "${RED}Error: Python 3.11+ required${NC}"
        return 1
    }
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

# A child process can't activate a venv in the parent shell, so make
# activation unnecessary instead: symlink the tools' entry points into
# ~/.local/bin (the pipx approach) — they run from the venv without it
# being active.
link_venv_tools() {
    [ -n "$VENV_USED" ] || return 0
    local bin_dir="$HOME/.local/bin"
    mkdir -p "$bin_dir"
    local linked=""
    local t
    for t in $TOOL_BINS; do
        if [ -x "$VENV_USED/bin/$t" ]; then
            ln -sf "$VENV_USED/bin/$t" "$bin_dir/$t"
            linked="$linked $t"
        fi
    done
    [ -n "$linked" ] || return 0
    echo "Symlinked into $bin_dir (no venv activation needed):"
    echo " $linked"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *)
            echo -e "${YELLOW}note:${NC} $bin_dir is not on your PATH; add it:"
            echo "  export PATH=\"$bin_dir:\$PATH\""
            ;;
    esac
}

# Remove ~/.local/bin symlinks that point into the given venv
unlink_venv_tools() {
    local venv="$1"
    local bin_dir="$HOME/.local/bin"
    local t link target
    for t in $TOOL_BINS; do
        link="$bin_dir/$t"
        [ -L "$link" ] || continue
        target="$(readlink "$link")"
        case "$target" in
            "$venv"/*) rm -f "$link"; echo "  removed $link" ;;
        esac
    done
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

    # Install gerrit_dashboard (needs gerrit_cli, installed just above)
    echo ""
    echo "Installing gerrit-dashboard..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/gerrit_dashboard"
    echo -e "${GREEN}✓${NC} gerrit-dashboard installed"

    # Install patch_watcher (the agent session console)
    echo ""
    echo "Installing patch-watcher..."
    $PYTHON -m pip install -q -e "$SCRIPT_DIR/patch_watcher"
    echo -e "${GREEN}\u2713${NC} patch-watcher installed"

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
    if [[ "$(uname -s)" == "Darwin" && -z "${LLM_TOOLS_TRY_DRGN:-}" ]]; then
        # drgn ships no macOS wheels, its source build uses Linux-only
        # APIs (os.sched_getaffinity), and the required elfutils has no
        # Homebrew bottle — the install cannot succeed today.
        echo ""
        echo -e "${YELLOW}Skipping drgn/lustre-drgn-tools on macOS${NC} (drgn is effectively"
        echo "Linux-only: no macOS wheels, source build fails). It is only"
        echo "needed for vmcore analysis — use a Linux host for that, or"
        echo "set LLM_TOOLS_TRY_DRGN=1 to attempt the install anyway."
    elif [[ -d "$SCRIPT_DIR/lustre-drgn-tools" ]]; then
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

    echo ""
    echo "========================================"
    echo -e "${GREEN}Installation Complete!${NC}"
    echo "========================================"
    echo ""
    if [ -n "$VENV_USED" ]; then
        echo "Tools are installed in a virtual environment:"
        echo "  $VENV_USED"
        link_venv_tools
        if [ -n "${INSTALL_SH_VENV_FILE:-}" ]; then
            printf '%s\n' "$VENV_USED" > "$INSTALL_SH_VENV_FILE" \
                2>/dev/null || true
        else
            echo ""
            echo -e "${YELLOW}To activate the venv in this shell, run:${NC}"
            echo ""
            echo "  source $VENV_USED/bin/activate"
            echo ""
            echo "(or run 'source install.sh' next time to finish with it"
            echo "activated automatically; the ~/.local/bin symlinks above"
            echo "work without any activation)"
        fi
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
    echo ""
    echo "Verify installation:"
    echo "  jira --help"
    echo "  gerrit --help"
    echo "  maloo --help"
    echo "  jenkins --help"
    echo "  lustre-crash --help"
    echo "  janitor --help"
    echo "  lreview check"
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
        echo "Removing ~/.local/bin symlinks into it..."
        unlink_venv_tools "$venv"
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

    echo "Uninstalling gerrit-dashboard..."
    $PYTHON -m pip uninstall -y gerrit-dashboard 2>/dev/null || true

    # Removing only the ~/.local/bin symlinks above left the editable install
    # itself in place: invisible, still importable, and dangling as soon as
    # the checkout it points at is deleted.
    echo "Uninstalling patch-watcher..."
    $PYTHON -m pip uninstall -y patch-watcher 2>/dev/null || true

    echo "Uninstalling lustre-crash..."
    $PYTHON -m pip uninstall -y lustre-crash 2>/dev/null || true
    $PYTHON -m pip uninstall -y crash-tool 2>/dev/null || true

    echo "Uninstalling llm-tool-common..."
    $PYTHON -m pip uninstall -y llm-tool-common 2>/dev/null || true

    echo ""
    echo -e "${GREEN}✓${NC} Python tools uninstalled"
    echo ""
}

install_ltvm() {
    # ltvm lives in its own repository; Patch Watcher agents shell out to it to
    # create the VMs they build and test in.
    local ltvm_repo="${LTVM_REPO:-$HOME/lustre-test-vms-v2}"
    echo ""
    echo "Installing ltvm..."
    if command -v ltvm >/dev/null 2>&1; then
        echo -e "${GREEN}\u2713${NC} ltvm already on PATH: $(command -v ltvm)"
        return 0
    fi
    if [ ! -d "$ltvm_repo" ]; then
        echo -e "${YELLOW}!${NC} lustre-test-vms-v2 not found at $ltvm_repo"
        echo "  Clone it, then re-run with --with-ltvm, or set LTVM_REPO."
        return 1
    fi
    # lustre-test-vms-v2 has no install.sh -- we looked for one that has never
    # existed, so --with-ltvm was a no-op on every host. Its documented
    # installer is `make install`, whose whole body is `sudo ./ltvm install`
    # (Makefile "install" target); calling that directly avoids also requiring
    # make.
    if [ ! -x "$ltvm_repo/ltvm" ]; then
        echo -e "${YELLOW}!${NC} $ltvm_repo/ltvm is missing or not executable"
        echo "  Check the clone is complete, or set LTVM_REPO."
        return 1
    fi
    # `ltvm install` writes /usr/local/bin and /etc, so it needs root.
    local elevate=""
    if [ "$(id -u)" -ne 0 ]; then
        if ! command -v sudo >/dev/null 2>&1; then
            echo -e "${YELLOW}!${NC} ltvm install needs root and sudo is not available"
            echo "  Run as root:  $ltvm_repo/ltvm install"
            return 1
        fi
        elevate="sudo"
    fi
    (cd "$ltvm_repo" && $elevate ./ltvm install) || return 1
    hash -r 2>/dev/null || true
    if ! command -v ltvm >/dev/null 2>&1; then
        echo -e "${YELLOW}!${NC} ltvm install finished but ltvm is still not on PATH"
        echo "  Expected /usr/local/bin/ltvm; check the output above."
        return 1
    fi
    echo -e "${GREEN}\u2713${NC} ltvm installed: $(command -v ltvm)"
}

# Credential setup is bash and needs no interpreter, no install and no
# network: it writes the same ~/.config/<tool>/.env files the CLIs already
# read, so it works on a host where nothing is installed yet.
run_configure() {
    if [ ! -t 0 ]; then
        configure_summary
        echo ""
        echo "Setting credentials needs a terminal; run it interactively:"
        echo "  ./install.sh --configure"
        return 2
    fi
    configure_tools "$ONLY_TOOLS"
}

run_doctor() {
    if command -v pw-doctor >/dev/null 2>&1; then
        pw-doctor "$@"
    else
        (cd "$SCRIPT_DIR/patch_watcher" && "$PYTHON" -m patch_watcher.pw_doctor "$@")
    fi
}

# ---------------------------------------------------------------------------
# Credential configuration
#
# jira, gerrit, maloo and jenkins each read a private KEY=VALUE file under
# ~/.config.  The walkthrough below takes one tool at a time: what it is for,
# what it needs, where that credential comes from, and "not now" as a
# first-class answer -- an unconfigured tool costs nothing until it is used.
# ---------------------------------------------------------------------------

CONFIG_TOOLS="gerrit jira maloo jenkins"

# Per-tool metadata, returned in SPEC_* globals.  SPEC_FIELDS is one
# "KEY|prompt|kind|default" record per line; kind is url, text or secret.
tool_spec() {
    SPEC_LABEL=""
    SPEC_FILE=""
    SPEC_USED_BY=""
    SPEC_FIELDS=""
    SPEC_REQUIRED=""
    SPEC_WHERE=""
    SPEC_NOTE=""
    case "$1" in
        gerrit)
            SPEC_LABEL="Gerrit"
            SPEC_USED_BY="gerrit (gc), lreview"
            SPEC_FILE="$HOME/.config/gerrit-cli/.env"
            SPEC_REQUIRED="GERRIT_URL GERRIT_USER GERRIT_PASS"
            SPEC_FIELDS="GERRIT_URL|Gerrit URL|url|https://review.whamcloud.com
GERRIT_USER|Gerrit username|text|
GERRIT_PASS|Gerrit HTTP password|secret|"
            SPEC_WHERE="Log in to Gerrit, then Settings > HTTP Credentials >
    Generate Password.  It is that generated password, not the one
    you log into the web UI with."
            ;;
        jira)
            SPEC_LABEL="Jira"
            SPEC_USED_BY="jira"
            SPEC_FILE="$HOME/.config/jira-tool/.env"
            SPEC_REQUIRED="JIRA_SERVER JIRA_TOKEN"
            SPEC_FIELDS="JIRA_SERVER|Jira URL|url|https://jira.whamcloud.com
JIRA_TOKEN|Jira personal access token|secret|"
            SPEC_WHERE="Jira > your avatar > Profile > Personal Access Tokens >
    Create token.  For Atlassian Cloud instead, an API token from
    https://id.atlassian.com/manage-profile/security/api-tokens"
            SPEC_NOTE="No username: the token is the whole login.  Several Jira
        instances at once go in ~/.jira-tool.json -- see README.md."
            ;;
        maloo)
            SPEC_LABEL="Maloo"
            SPEC_USED_BY="maloo"
            SPEC_FILE="$HOME/.config/maloo-tool/.env"
            SPEC_REQUIRED="MALOO_USER MALOO_PASS"
            SPEC_FIELDS="MALOO_URL|Maloo URL|url|https://testing.whamcloud.com
MALOO_USER|Maloo username|text|
MALOO_PASS|Maloo password|secret|"
            SPEC_WHERE="The account you sign in to https://testing.whamcloud.com
    with -- there is no separate token.  Ask Whamcloud for an
    account if you do not have one."
            ;;
        jenkins)
            SPEC_LABEL="Jenkins"
            SPEC_USED_BY="jenkins"
            SPEC_FILE="$HOME/.config/jenkins-tool/.env"
            SPEC_REQUIRED="JENKINS_USER JENKINS_TOKEN"
            SPEC_FIELDS="JENKINS_URL|Jenkins URL|url|https://build.whamcloud.com
JENKINS_USER|Jenkins username|text|
JENKINS_TOKEN|Jenkins API token|secret|"
            SPEC_WHERE="Log in to Jenkins, then your name (top right) >
    Configure > API Token > Add new Token.  Copy it before
    leaving the page; Jenkins shows it once."
            ;;
        *)
            return 1
            ;;
    esac
}

# Shorten $HOME to ~ for display.  These paths are printed a dozen times and
# the full home directory buries the part that matters.
tilde_path() {
    case "$1" in
        "$HOME"/*) printf '~%s' "${1#$HOME}" ;;
        *) printf '%s' "$1" ;;
    esac
}

# Print the value of KEY in a KEY=VALUE file, or nothing.  Always succeeds:
# under `set -e` a failing command substitution in an assignment would end
# the script.
env_file_get() {
    local file="$1" key="$2" line value
    [ -f "$file" ] || return 0
    line=$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1) || true
    [ -n "$line" ] || return 0
    value="${line#*=}"
    printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Merge the KEY=VALUE lines in $2 into the file at $1, leaving comments and
# any keys we do not manage untouched.  Written to a temp file in the same
# (0700) directory and renamed, so an interrupted write cannot leave a
# truncated credential file behind.
env_file_write() {
    local file="$1" pairs="$2" dir source tmp pairfile
    dir=$(dirname "$file")
    mkdir -p "$dir"
    chmod 700 "$dir" 2>/dev/null || true
    pairfile=$(mktemp "$dir/.pairs.XXXXXX") || return 1
    chmod 600 "$pairfile"
    printf '%s' "$pairs" > "$pairfile"
    tmp=$(mktemp "$dir/.env.XXXXXX") || { rm -f "$pairfile"; return 1; }
    chmod 600 "$tmp"
    source="$file"
    [ -f "$source" ] || source="/dev/null"
    awk '
        NR == FNR {
            if (match($0, /^[A-Za-z_][A-Za-z0-9_]*=/)) {
                k = substr($0, 1, RLENGTH - 1)
                value[k] = $0
                order[++n] = k
            }
            next
        }
        {
            k = $0
            sub(/[[:space:]]*=.*$/, "", k)
            sub(/^[[:space:]]*/, "", k)
            if (k in value) {
                if (!(k in done)) {
                    print value[k]
                    done[k] = 1
                }
                next
            }
            print $0
        }
        END {
            for (i = 1; i <= n; i++)
                if (!(order[i] in done))
                    print value[order[i]]
        }
    ' "$pairfile" "$source" > "$tmp" || { rm -f "$pairfile" "$tmp"; return 1; }
    rm -f "$pairfile"
    mv "$tmp" "$file" || { rm -f "$tmp"; return 1; }
    chmod 600 "$file"
}

# Required keys of $1 that are absent or empty.
tool_missing_keys() {
    local tool="$1" key out=""
    tool_spec "$tool" || return 0
    for key in $SPEC_REQUIRED; do
        [ -n "$(env_file_get "$SPEC_FILE" "$key")" ] || out="$out $key"
    done
    printf '%s' "${out# }"
}

# configured | partial | missing
tool_status() {
    local tool="$1" missing
    tool_spec "$tool" || return 0
    missing=$(tool_missing_keys "$tool")
    if [ -z "$missing" ]; then
        echo configured
    elif [ ! -f "$SPEC_FILE" ] || [ "$missing" = "$SPEC_REQUIRED" ]; then
        echo missing
    else
        echo partial
    fi
}

# Where else the tools would find this credential.  llm_tool_common's loader
# also reads the environment, /etc/<tool>/.env, /shared/support_files/.env and
# ./.env, and jira keeps multi-instance config in ~/.jira-tool.json -- so a
# host can be working perfectly with nothing in the file this script writes.
# Reporting that as "not configured" would be a lie.
tool_external_source() {
    local tool="$1" key dir alt complete
    tool_spec "$tool" || return 0
    complete=1
    for key in $SPEC_REQUIRED; do
        [ -n "${!key:-}" ] || complete=0
    done
    if [ "$complete" = "1" ]; then
        printf '%s' "your environment"
        return 0
    fi
    dir=$(basename "$(dirname "$SPEC_FILE")")
    for alt in "/etc/$dir/.env" "/shared/support_files/.env" "$PWD/.env"; do
        [ -f "$alt" ] || continue
        complete=1
        for key in $SPEC_REQUIRED; do
            [ -n "$(env_file_get "$alt" "$key")" ] || complete=0
        done
        if [ "$complete" = "1" ]; then
            printf '%s' "$alt"
            return 0
        fi
    done
    if [ "$tool" = "jira" ] && [ -f "$HOME/.jira-tool.json" ] &&
        grep -q '"token"' "$HOME/.jira-tool.json" 2>/dev/null; then
        printf '%s' "$HOME/.jira-tool.json"
    fi
    return 0
}

curl_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# Check a credential against the server it is for.  Prints the HTTP status
# (or no-curl), and succeeds only on 200.  The secret reaches curl through a
# config file on stdin, never on the command line, where ps would show it.
tool_probe() {
    local tool="$1" url user pass token config code
    if ! command -v curl >/dev/null 2>&1; then
        echo "no-curl"
        return 2
    fi
    tool_spec "$tool" || return 2
    case "$tool" in
        gerrit)
            url=$(env_file_get "$SPEC_FILE" GERRIT_URL)
            user=$(env_file_get "$SPEC_FILE" GERRIT_USER)
            pass=$(env_file_get "$SPEC_FILE" GERRIT_PASS)
            config="url = \"$(curl_escape "${url%/}/a/accounts/self")\"
user = \"$(curl_escape "$user:$pass")\""
            ;;
        jira)
            url=$(env_file_get "$SPEC_FILE" JIRA_SERVER)
            token=$(env_file_get "$SPEC_FILE" JIRA_TOKEN)
            config="url = \"$(curl_escape "${url%/}/rest/api/2/myself")\"
header = \"Authorization: Bearer $(curl_escape "$token")\""
            ;;
        maloo)
            url=$(env_file_get "$SPEC_FILE" MALOO_URL)
            [ -n "$url" ] || url="https://testing.whamcloud.com"
            user=$(env_file_get "$SPEC_FILE" MALOO_USER)
            pass=$(env_file_get "$SPEC_FILE" MALOO_PASS)
            config="url = \"$(curl_escape "${url%/}/api/test_sessions?limit=1")\"
user = \"$(curl_escape "$user:$pass")\""
            ;;
        jenkins)
            url=$(env_file_get "$SPEC_FILE" JENKINS_URL)
            [ -n "$url" ] || url="https://build.whamcloud.com"
            user=$(env_file_get "$SPEC_FILE" JENKINS_USER)
            token=$(env_file_get "$SPEC_FILE" JENKINS_TOKEN)
            config="url = \"$(curl_escape "${url%/}/api/json?tree=nodeName")\"
user = \"$(curl_escape "$user:$token")\""
            ;;
        *)
            echo "no-probe"
            return 2
            ;;
    esac
    code=$(printf '%s\n' "$config" |
        curl -sS -m 20 -o /dev/null -w '%{http_code}' -K - 2>/dev/null) || code="000"
    echo "$code"
    [ "$code" = "200" ]
}

ask_yes() {
    local prompt="$1" default="$2" answer suffix
    if [ "$default" = "y" ]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
    printf '%s %s ' "$prompt" "$suffix" >&2
    read -r answer || answer=""
    answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        y|yes) return 0 ;;
        *) return 1 ;;
    esac
}

# Ask for one field.  Prints the answer; returns 2 if the operator typed
# "skip", which abandons the whole tool without writing anything.
#
# The prompt is printed here rather than passed to `read -p`, which bash
# suppresses whenever stdin is not a terminal -- that silently swallows every
# prompt under a pipe.  stderr keeps it clear of the answer on stdout.
prompt_field() {
    local prompt="$1" kind="$2" shown="$3" answer="" suffix=""
    [ -z "$shown" ] || suffix=" [$shown]"
    printf '%s%s: ' "$prompt" "$suffix" >&2
    if [ "$kind" = "secret" ]; then
        read -r -s answer || answer=""
        echo "" >&2
    else
        read -r answer || answer=""
    fi
    case "$answer" in
        skip|SKIP|Skip) return 2 ;;
    esac
    printf '%s' "$answer" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

configure_one_tool() {
    local tool="$1" status key prompt kind default current shown answer rc
    local pairs code attempt=0 last_code="" from_env external
    status=$(tool_status "$tool")
    tool_spec "$tool" || return 0

    echo ""
    echo "--------------------------------------------------------------"
    echo " $SPEC_LABEL   (used by: $SPEC_USED_BY)"
    echo "--------------------------------------------------------------"
    echo "  file:  $(tilde_path "$SPEC_FILE")"
    echo "  where to get it:"
    echo "    $SPEC_WHERE"
    [ -z "$SPEC_NOTE" ] || echo "  note: $SPEC_NOTE"
    echo ""

    if [ "$status" != "configured" ]; then
        external=$(tool_external_source "$tool")
        if [ -n "$external" ]; then
            echo -e "  ${GREEN}already working${NC} via $(tilde_path "$external")"
            echo "  Setting it up here as well puts it where a tool started"
            echo "  outside your shell -- an agent, a cron job -- also finds it."
        fi
    fi

    case "$status" in
        configured)
            echo -e "  ${GREEN}already configured${NC}"
            if [ "${RECONFIGURE:-0}" != "1" ]; then
                echo "  (re-enter it with: ./install.sh --configure --only $tool --reconfigure)"
                return 0
            fi
            ;;
        partial)
            echo -e "  ${YELLOW}incomplete${NC} -- missing: $(tool_missing_keys "$tool")"
            ;;
    esac

    if ! ask_yes "  Set up $SPEC_LABEL now?" y; then
        echo "  Left for later:  ./install.sh --configure --only $tool"
        return 0
    fi

    while :; do
        attempt=$((attempt + 1))
        pairs=""
        # The field list comes in on fd 3: on stdin it would be what the
        # prompts below read from, and the operator would answer nothing.
        while IFS='|' read -r key prompt kind default <&3; do
            [ -n "$key" ] || continue
            current=$(env_file_get "$SPEC_FILE" "$key")
            from_env=""
            if [ -z "$current" ] && [ -n "${!key:-}" ]; then
                # Already exported in the operator's shell.  The CLIs read the
                # environment first, so offering it here just writes down what
                # they are already using -- which is what an agent running
                # without that shell needs.
                current="${!key}"
                from_env=" from environment"
            fi
            [ -n "$current" ] || current="$default"
            shown="$current$from_env"
            if [ "$kind" = "secret" ] && [ -n "$current" ]; then
                # After a rejection the stored secret is the one that just
                # failed; offering it as a bare "****" invites the operator
                # to press Enter and retry the same wrong value.  An
                # unreachable server says nothing about the secret, so only a
                # real rejection is labelled one.
                case "$last_code" in
                    401|403) shown="**** rejected" ;;
                    *) shown="****$from_env" ;;
                esac
            fi
            answer=$(prompt_field "    $prompt" "$kind" "$shown") && rc=0 || rc=$?
            if [ "$rc" = "2" ]; then
                echo "  Skipped $SPEC_LABEL -- nothing written."
                echo "  Later:  ./install.sh --configure --only $tool"
                return 0
            fi
            [ -n "$answer" ] || answer="$current"
            case " $SPEC_REQUIRED " in
                *" $key "*)
                    if [ -z "$answer" ]; then
                        echo -e "  ${YELLOW}$key is required${NC} -- leaving $SPEC_LABEL unconfigured."
                        echo "  Later:  ./install.sh --configure --only $tool"
                        return 0
                    fi
                    ;;
            esac
            [ -z "$answer" ] || pairs="$pairs$key=$answer
"
        done 3<<EOF
$SPEC_FIELDS
EOF

        if ! env_file_write "$SPEC_FILE" "$pairs"; then
            echo -e "  ${RED}could not write $(tilde_path "$SPEC_FILE")${NC}"
            return 0
        fi
        echo -e "  ${GREEN}wrote $(tilde_path "$SPEC_FILE")${NC} (mode 0600)"

        [ "${VERIFY:-1}" = "1" ] || return 0
        printf '  checking against the server... '
        if code=$(tool_probe "$tool"); then
            echo -e "${GREEN}ok${NC}"
            return 0
        fi
        last_code="$code"
        case "$code" in
            no-curl|no-probe)
                echo "skipped (curl not installed)"
                return 0
                ;;
            000)
                echo -e "${YELLOW}could not reach the server${NC} -- check the URL,"
                echo "    your network or VPN.  The values are saved either way."
                ;;
            401|403)
                echo -e "${RED}rejected (HTTP $code)${NC} -- the username or secret is wrong."
                ;;
            *)
                echo -e "${YELLOW}unexpected HTTP $code${NC} -- saved, but unverified."
                ;;
        esac
        ask_yes "  Enter $SPEC_LABEL again?" n || return 0
    done
}

configure_summary() {
    local tool status pad external
    echo ""
    echo "Credential status:"
    for tool in $CONFIG_TOOLS; do
        status=$(tool_status "$tool")
        tool_spec "$tool" || continue
        pad=$(printf '%-8s' "$tool")
        case "$status" in
            configured)
                echo -e "  ${GREEN}ok${NC}    $pad $(tilde_path "$SPEC_FILE")"
                ;;
            *)
                external=$(tool_external_source "$tool")
                if [ -n "$external" ]; then
                    echo -e "  ${GREEN}ok${NC}    $pad via $(tilde_path "$external")"
                elif [ "$status" = "partial" ]; then
                    echo -e "  ${YELLOW}part${NC}  $pad missing $(tool_missing_keys "$tool")" \
                        "-- ./install.sh --configure --only $tool"
                else
                    echo -e "  ${YELLOW}--${NC}    $pad not configured" \
                        "-- ./install.sh --configure --only $tool"
                fi
                ;;
        esac
    done
    echo ""
    echo "  janitor and lustre-crash need no credentials."
    echo "  lreview: run 'lreview setup' -- it reuses the Gerrit credentials"
    echo "           above and walks through the agent CLI and review prompts."
    echo ""
    echo "Check by hand:  gerrit info <change-url>   jira get LU-1"
    echo "                maloo queue                jenkins jobs"
}

configure_tools() {
    local only="$1" tool
    echo ""
    echo "========================================"
    echo "Credentials"
    echo "========================================"
    echo ""
    echo "One tool at a time.  Enter accepts the value in [brackets], 'n' at"
    echo "the first question leaves a tool for later, and 'skip' at any prompt"
    echo "abandons just that tool.  Files are written 0600 under ~/.config and"
    echo "nothing is sent anywhere except the server the credential is for."
    for tool in $CONFIG_TOOLS; do
        if [ -n "$only" ]; then
            case " $only " in
                *" $tool "*) ;;
                *) continue ;;
            esac
        fi
        configure_one_tool "$tool" || true
    done
    configure_summary
}

# Allow sourcing the functions without running the installer (tests).  This
# guard sat above install_ltvm/run_configure/run_doctor, so the three
# functions that carried the --configure, --doctor and --with-ltvm bugs were
# exactly the three no test could reach.
if [ -n "${INSTALL_SH_NO_MAIN:-}" ]; then return 0 2>/dev/null || exit 0; fi

# Parse arguments
ACTION="install"
WITH_LTVM=0
VENV_FLAG=0
VENV_PATH=""
ONLY_TOOLS=""
RECONFIGURE=0
VERIFY=1
CONFIGURE_AFTER_INSTALL=1
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --uninstall)
            ACTION="uninstall"
            ;;
        --configure)
            ACTION="configure"
            ;;
        --status)
            ACTION="status"
            ;;
        --only)
            if [ -z "${2:-}" ]; then
                echo -e "${RED}--only needs a tool name${NC} ($CONFIG_TOOLS)"
                exit 1
            fi
            case " $CONFIG_TOOLS " in
                *" $2 "*) ONLY_TOOLS="$ONLY_TOOLS $2" ;;
                *)
                    echo -e "${RED}Unknown tool: $2${NC} (known: $CONFIG_TOOLS)"
                    exit 1
                    ;;
            esac
            shift
            ;;
        --reconfigure)
            RECONFIGURE=1
            ;;
        --no-verify)
            VERIFY=0
            ;;
        --no-configure)
            CONFIGURE_AFTER_INSTALL=0
            ;;
        --doctor)
            ACTION="doctor"
            ;;
        --with-ltvm)
            WITH_LTVM=1
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

ONLY_TOOLS="${ONLY_TOOLS# }"
# --only/--reconfigure on their own mean "configure that": asking for one
# tool and silently getting a full reinstall would be a nasty surprise.
if [ "$ACTION" = "install" ] && { [ -n "$ONLY_TOOLS" ] || [ "$RECONFIGURE" = "1" ]; }; then
    ACTION="configure"
fi

case "$ACTION" in
    uninstall)
        uninstall_tools
        ;;
    configure)
        configure_rc=0
        run_configure || configure_rc=$?
        exit $configure_rc
        ;;
    status)
        configure_summary
        ;;
    doctor)
        require_runtime_python || exit 1
        run_doctor
        ;;
    *)
        install_tools
        ltvm_failed=0
        if [ "$WITH_LTVM" = "1" ]; then
            install_ltvm || ltvm_failed=1
        fi
        if [ "$CONFIGURE_AFTER_INSTALL" = "1" ] && [ -t 0 ]; then
            configure_tools "$ONLY_TOOLS"
        else
            configure_summary
            echo ""
            echo "Set them up any time with:  ./install.sh --configure"
        fi
        # The Python tools are installed either way, but the operator asked
        # for ltvm and agents cannot create VMs without it, so do not report
        # success.
        if [ "$ltvm_failed" = "1" ]; then
            echo ""
            echo -e "${RED}--with-ltvm was requested but ltvm was not installed${NC} (see above)."
            exit 1
        fi
        ;;
esac
