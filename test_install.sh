#!/bin/bash
#
# Tests for install.sh: the credential walkthrough and the Claude skills.
#
# Everything runs against a temporary HOME with the network check turned off;
# no real credential file or skill is read or written.  Run it with:
#   ./test_install.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$SCRIPT_DIR/install.sh"
PASS=0
FAIL=0
TRASH=""

# Run from a scratch directory: the tools' loader also reads ./.env, so a
# stray one in the checkout would change what these tests see.
WORK=$(mktemp -d)
cd "$WORK" || exit 1
cleanup() { rm -rf $TRASH "$WORK"; }
trap cleanup EXIT

ok() {
    PASS=$((PASS + 1))
    echo "  ok    $1"
}

bad() {
    FAIL=$((FAIL + 1))
    echo "  FAIL  $1"
    [ -z "${2:-}" ] || echo "        $2"
}

check() {  # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$2], got [$3]"; fi
}

contains() {  # contains <description> <needle> <haystack>
    case "$3" in
        *"$2"*) ok "$1" ;;
        *) bad "$1" "[$2] not in output" ;;
    esac
}

fresh_home() {
    HOME_DIR=$(mktemp -d)
    TRASH="$TRASH $HOME_DIR"
}

echo "install.sh"

# --- a fully answered tool is written where the CLI reads it ---------------
fresh_home
out=$(printf 'y\nhttps://gerrit.example\nalice\nhunter2\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
file="$HOME_DIR/.config/gerrit-cli/.env"
if [ -f "$file" ]; then
    ok "a fully answered tool is written"
    check "  file is private" "600" "$(stat -c %a "$file")"
    check "  directory is private" "700" "$(stat -c %a "$(dirname "$file")")"
    check "  values land under the keys the CLI reads" \
        "GERRIT_URL=https://gerrit.example GERRIT_USER=alice GERRIT_PASS=hunter2" \
        "$(tr '\n' ' ' < "$file" | sed 's/ $//')"
else
    bad "a fully answered tool is written" "$out"
fi

# --- "not now" writes nothing and says how to come back --------------------
fresh_home
out=$(printf 'n\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jira'" 2>&1)
# Declining leaves a tool that still reads: the server is not a secret,
# and without it an anonymous read has nothing to talk to.
check "answering no writes the server and nothing else" \
    "JIRA_SERVER=https://jira.whamcloud.com" \
    "$(cat "$HOME_DIR/.config/jira-tool/.env")"
contains "answering no names the command to come back with" \
    "--configure --only jira" "$out"

# --- the two Jiras: Whamcloud first, Atlassian Cloud after -----------------
# Both land in one .env, so Cloud's keys must sit beside the server's rather
# than on top of them, and Cloud needs an email where the server needs none.
fresh_home
out=$(printf 'y\n\nwctoken\ny\nhttps://acme.atlassian.net\nme@acme.com\ncloudtoken\nACME,FOO\n' |
    env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jira jira-cloud'" 2>&1)
wc_line=$(printf '%s\n' "$out" | grep -n -m1 'Jira Server (Whamcloud)' | cut -d: -f1)
cloud_line=$(printf '%s\n' "$out" | grep -n -m1 'Jira Cloud (Atlassian)' | cut -d: -f1)
if [ -n "$wc_line" ] && [ -n "$cloud_line" ] && [ "$wc_line" -lt "$cloud_line" ]; then
    ok "Whamcloud Jira is asked for before Atlassian Cloud"
else
    bad "Whamcloud Jira is asked for before Atlassian Cloud" "$out"
fi
check "  both Jiras land in one file, each under its own keys" \
    "JIRA_SERVER=https://jira.whamcloud.com JIRA_TOKEN=wctoken JIRA_CLOUD_SERVER=https://acme.atlassian.net JIRA_CLOUD_EMAIL=me@acme.com JIRA_CLOUD_TOKEN=cloudtoken JIRA_CLOUD_PROJECTS=ACME,FOO" \
    "$(tr '\n' ' ' < "$HOME_DIR/.config/jira-tool/.env" | sed 's/ $//')"

fresh_home
out=$(printf 'y\n\nwctoken\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jira jira-cloud'" 2>&1)
check "declining Cloud adds nothing to the Whamcloud file" \
    "JIRA_SERVER=https://jira.whamcloud.com JIRA_TOKEN=wctoken" \
    "$(tr '\n' ' ' < "$HOME_DIR/.config/jira-tool/.env" | sed 's/ $//')"

# Cloud credentials are reached only through a project prefix, so without
# one they are written and never used.
fresh_home
out=$(printf 'y\nhttps://acme.atlassian.net\nme@acme.com\ncloudtoken\n\n' |
    env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jira-cloud'" 2>&1)
contains "Cloud credentials with no project keys say they are inert" \
    "no project keys given" "$out"

# --- "skip" part-way through abandons the tool, writing nothing ------------
fresh_home
out=$(printf 'y\n\nskip\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'maloo'" 2>&1)
if [ -e "$HOME_DIR/.config/maloo-tool/.env" ]; then
    bad "skip half way through writes nothing"
else
    ok "skip half way through writes nothing"
fi

# --- a required field left empty does not write a half-configured file -----
fresh_home
out=$(printf 'y\n\n\n\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
check "an unanswered credential writes the server, not half a login" \
    "GERRIT_URL=https://review.whamcloud.com" \
    "$(cat "$HOME_DIR/.config/gerrit-cli/.env")"
contains "and says the tool is left read-only" "read-only" "$out"

# --- an existing file keeps its comments, its other keys and its values ----
fresh_home
mkdir -p "$HOME_DIR/.config/jenkins-tool"
printf '# notes\nJENKINS_URL=https://build.example\nUNRELATED=keep-me\nJENKINS_USER=bob\n' \
    > "$HOME_DIR/.config/jenkins-tool/.env"
out=$(printf 'y\n\n\nnewtoken\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jenkins'" 2>&1)
check "an existing file is merged, not replaced" \
    "# notes JENKINS_URL=https://build.example UNRELATED=keep-me JENKINS_USER=bob JENKINS_TOKEN=newtoken" \
    "$(tr '\n' ' ' < "$HOME_DIR/.config/jenkins-tool/.env" | sed 's/ $//')"
contains "a part-configured tool says what is missing" "missing: JENKINS_TOKEN" "$out"

# --- an already configured tool is left alone unless asked -----------------
out=$(printf '' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jenkins'" 2>&1)
contains "an already configured tool is left alone" "already configured" "$out"
contains "and names the flag that re-prompts" "--reconfigure" "$out"

# --- a value already exported in the shell is offered as the default -------
fresh_home
out=$(printf 'y\n\n\n\n' | env HOME="$HOME_DIR" VERIFY=0 \
    GERRIT_USER=envuser GERRIT_PASS=envsecret \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
contains "an exported value is offered as the default" "envuser from environment" "$out"
check "  and accepting it writes it down" "envsecret" \
    "$(sed -n 's/^GERRIT_PASS=//p' "$HOME_DIR/.config/gerrit-cli/.env")"

# --- the keys prompted for are the keys the CLIs actually read -------------
# The bug this guards: a walkthrough that writes JIRA_URL into a file whose
# reader only looks for JIRA_SERVER configures nothing at all.
key_source() {
    case "$1" in
        gerrit) echo "$SCRIPT_DIR/gerrit_cli/gerrit_cli/client.py" ;;
        jira) echo "$SCRIPT_DIR/jira_tool/jira_tool/config.py" ;;
        jira-cloud) echo "$SCRIPT_DIR/jira_tool/jira_tool/commands/_helpers.py" ;;
        maloo) echo "$SCRIPT_DIR/maloo_tool/maloo_tool/config.py" ;;
        jenkins) echo "$SCRIPT_DIR/jenkins_tool/jenkins_tool/config.py" ;;
    esac
}
INSTALL_SH_NO_MAIN=1 source "$INSTALL_SH"
set +e   # install.sh sets -e, which sourcing hands to this shell
for tool in $CONFIG_TOOLS; do
    tool_spec "$tool"
    reader=$(key_source "$tool")
    if [ ! -f "$reader" ]; then
        bad "$tool: cannot find the module that reads its keys" "$reader"
        continue
    fi
    unknown=""
    while IFS='|' read -r key rest; do
        [ -n "$key" ] || continue
        grep -q "\"$key\"" "$reader" || unknown="$unknown $key"
    done <<EOF
$SPEC_FIELDS
EOF
    if [ -z "$unknown" ]; then
        ok "$tool prompts only for keys $(basename "$(dirname "$reader")") reads"
    else
        bad "$tool prompts for keys nothing reads:$unknown"
    fi
done

# --- a wrong credential is caught on the spot, and can be re-entered -------
if command -v curl >/dev/null 2>&1; then
    cat > "$WORK/fake_gerrit.py" <<'SERVER'
import base64, http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        ok = False
        if auth.startswith("Basic "):
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            ok = (user, pw) == ("alice", "right")
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *a): pass
server = http.server.HTTPServer(("127.0.0.1", 0), H)
print(server.server_port, flush=True)
server.serve_forever()
SERVER
    exec 4< <(python3 "$WORK/fake_gerrit.py")
    read -r PORT <&4
    SERVER_PID=$!
    fresh_home
    out=$(printf 'y\nhttp://127.0.0.1:%s\nalice\nwrong\ny\n\n\nright\n' "$PORT" |
        env HOME="$HOME_DIR" bash -c \
        "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
    kill "$SERVER_PID" 2>/dev/null
    exec 4<&-
    contains "a wrong credential is rejected as it is entered" \
        "rejected (HTTP 401)" "$out"
    contains "the rejected secret is labelled on the retry" "**** rejected" "$out"
    contains "and a corrected one checks out" "checking against the server... ok" \
        "$(printf '%s' "$out" | tr -d '\r')"
    check "the corrected value is what was kept" "right" \
        "$(sed -n 's/^GERRIT_PASS=//p' "$HOME_DIR/.config/gerrit-cli/.env")"
else
    echo "  skip  credential check tests (no curl)"
fi

# --- credentials the tools find elsewhere are not called "not configured" ---
# The loader also reads the environment, /etc/<tool>/.env,
# /shared/support_files/.env and ./.env, and jira has its own JSON config.
fresh_home
out=$(env HOME="$HOME_DIR" MALOO_USER=alice MALOO_PASS=pw \
    bash "$INSTALL_SH" --status 2>&1)
contains "an exported credential is reported, not called missing" \
    "via your environment" "$out"

fresh_home
printf '{"server": "https://jira.example", "auth": {"token": "t"}}\n' \
    > "$HOME_DIR/.jira-tool.json"
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --status 2>&1)
contains "jira's own JSON config counts as configured" ".jira-tool.json" "$out"

# --- a second credential set ------------------------------------------------
# Extra accounts live in the same file under an [alias].  The default set has
# to survive that untouched, since it is what every command uses.
fresh_home
mkdir -p "$HOME_DIR/.config/gerrit-cli"
printf '# notes\nGERRIT_URL=https://review.example\nGERRIT_USER=alice\nGERRIT_PASS=main\nUNRELATED=keep-me\n' \
    > "$HOME_DIR/.config/gerrit-cli/.env"
out=$(printf 'y\ngerrit\nbot\n\nbotuser\nbotsecret\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
check "a second set is appended, leaving the first alone" \
    "# notes GERRIT_URL=https://review.example GERRIT_USER=alice GERRIT_PASS=main UNRELATED=keep-me  [bot] GERRIT_USER=botuser GERRIT_PASS=botsecret" \
    "$(tr '\n' ' ' < "$HOME_DIR/.config/gerrit-cli/.env" | sed 's/ $//')"
contains "and says how to reach it" "gerrit --user bot" "$out"
contains "and the status list names it with its account" "--user bot  (botuser)" "$out"

# A key left blank is inherited from the default set rather than written
# empty -- the common case is a second login on the same server.
if grep -A3 '^\[bot\]' "$HOME_DIR/.config/gerrit-cli/.env" | grep -q GERRIT_URL; then
    bad "a blank field is inherited, not written into the section"
else
    ok "a blank field is inherited, not written into the section"
fi

# Editing an existing set replaces its keys instead of appending a
# second [bot] block.
out=$(printf 'y\ngerrit\nbot\n\n\nnewsecret\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
check "editing a set rewrites it in place" "1" \
    "$(grep -c '^\[bot\]' "$HOME_DIR/.config/gerrit-cli/.env")"
check "  and keeps the value that was not retyped" "botuser" \
    "$(sed -n '/^\[bot\]/,$p' "$HOME_DIR/.config/gerrit-cli/.env" | sed -n 's/^GERRIT_USER=//p')"
check "  while taking the one that was" "newsecret" \
    "$(sed -n '/^\[bot\]/,$p' "$HOME_DIR/.config/gerrit-cli/.env" | sed -n 's/^GERRIT_PASS=//p')"

# The bug this guards: a reader that greps the whole file for GERRIT_USER
# picks up the one under [bot] and reports the wrong default account.
INSTALL_SH_NO_MAIN=1 source "$INSTALL_SH"
set +e
CONFIG_SECTION=""
check "the default set is read from before the first header" "alice" \
    "$(env_file_get "$HOME_DIR/.config/gerrit-cli/.env" GERRIT_USER)"
CONFIG_SECTION="bot"
check "and a named set from inside its own" "botuser" \
    "$(env_file_get "$HOME_DIR/.config/gerrit-cli/.env" GERRIT_USER)"
# The bug this guards: [bot] carries only a login, so a reader without
# inheritance hands tool_probe an empty URL and the credential check
# reports "could not reach the server" for a credential the server would
# have rejected outright.
check "a named set inherits what it does not define" "https://review.example" \
    "$(env_file_get "$HOME_DIR/.config/gerrit-cli/.env" GERRIT_URL)"
check "  and the raw read still sees only its own keys" "" \
    "$(env_file_get_raw "$HOME_DIR/.config/gerrit-cli/.env" GERRIT_URL bot)"
CONFIG_SECTION=""

# A named set shows what a blank answer would inherit, so the choice is
# visible.  For a secret that hint must be masked: printing the default
# account's password to explain Enter is worse than explaining nothing.
fresh_home
mkdir -p "$HOME_DIR/.config/gerrit-cli"
printf 'GERRIT_URL=https://review.example\nGERRIT_USER=alice\nGERRIT_PASS=SUPERSECRETVALUE\n' \
    > "$HOME_DIR/.config/gerrit-cli/.env"
out=$(printf 'y\ngerrit\nbot\n\nbotuser\n\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_extra_users" 2>&1)
contains "an inherited value is shown, so a blank answer is informed" \
    "alice -- inherited" "$out"
case "$out" in
    *SUPERSECRETVALUE*) bad "an inherited secret is never printed" ;;
    *) ok "an inherited secret is never printed" ;;
esac
contains "  it is masked instead" "**** -- inherited" "$out"

fresh_home
mkdir -p "$HOME_DIR/.config/gerrit-cli"
printf 'GERRIT_URL=https://review.example\nGERRIT_USER=alice\nGERRIT_PASS=main\n' \
    > "$HOME_DIR/.config/gerrit-cli/.env"
out=$(printf 'y\ngerrit\ndefault\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
contains "'default' is refused as an alias" "is the set configured above" "$out"

out=$(printf 'y\ngerrit\nbad name!\nn\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'gerrit'" 2>&1)
contains "an alias with odd characters is refused" "alias may use letters" "$out"

# jira and jira-cloud share one file, so a section belongs to whichever of
# them its keys are for.  Reporting a Jira Server account as a second Cloud
# site would send someone looking for an Atlassian site that is not there.
fresh_home
mkdir -p "$HOME_DIR/.config/jira-tool"
printf 'JIRA_SERVER=https://jira.whamcloud.com\nJIRA_TOKEN=t\n\n[bot]\nJIRA_SERVER=https://jira.whamcloud.com\nJIRA_TOKEN=bot-token\n' \
    > "$HOME_DIR/.config/jira-tool/.env"
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --status 2>&1)
jira_row=$(printf '%s\n' "$out" | grep -n -m1 '^  ok    jira  ' | cut -d: -f1)
cloud_row=$(printf '%s\n' "$out" | grep -n -m1 'jira-cloud' | cut -d: -f1)
set_row=$(printf '%s\n' "$out" | grep -n -m1 -- '--user bot' | cut -d: -f1)
if [ -n "$set_row" ] && [ -n "$jira_row" ] && [ -n "$cloud_row" ] &&
    [ "$set_row" -gt "$jira_row" ] && [ "$set_row" -lt "$cloud_row" ]; then
    ok "a shared file's set is listed under the tool whose keys it holds"
else
    bad "a shared file's set is listed under the tool whose keys it holds" "$out"
fi

# --- the command line ------------------------------------------------------
fresh_home
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --status 2>&1)
contains "--status reports an unconfigured host" "not configured" "$out"
contains "--status does not nag about an unused Cloud site" \
    "no Cloud site" "$out"

out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --configure < /dev/null 2>&1)
rc=$?
check "--configure without a terminal exits 2" "2" "$rc"
contains "--configure without a terminal says why" "needs a terminal" "$out"

out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --only bogus < /dev/null 2>&1)
rc=$?
check "--only rejects an unknown tool" "1" "$rc"
contains "--only lists the tools it knows" "gerrit jira jira-cloud maloo jenkins" "$out"

out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --only jira < /dev/null 2>&1)
if echo "$out" | grep -q "Installing"; then
    bad "--only on its own configures rather than installs"
else
    ok "--only on its own configures rather than installs"
fi

# --- a Python without pip ---------------------------------------------------
# Rocky/RHEL ship pip as its own package, so python3.11 can be new enough
# and still have no pip; the venv it can build has one.
venv_dir="$WORK/nopip-venv"
out=$(bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'
    has_pip() { return 1; }
    VENV_FLAG=0 VENV_PATH='$venv_dir' resolve_python python3 < /dev/null
    echo \"PYTHON=\$PYTHON\"" 2>&1)
contains "a pip-less Python says so rather than failing on pip" \
    "has no pip module" "$out"
contains "  and installs into a venv instead" "PYTHON=$venv_dir/bin/python" "$out"
if "$venv_dir/bin/python" -m pip --version > /dev/null 2>&1; then
    ok "  which has a pip of its own"
else
    bad "  which has a pip of its own" "$out"
fi
rm -rf "$venv_dir"

# --- the skills ------------------------------------------------------------
# They are linked, not copied, so a git pull updates them in place.
SKILLS_DIR="$SCRIPT_DIR/skills"
for skill in "$SKILLS_DIR"/*/; do
    [ -d "$skill" ] || continue
    name=$(basename "$skill")
    front=$(sed -n '2,/^---$/p' "$skill/SKILL.md")
    declared=$(printf '%s' "$front" | sed -n 's/^name: *//p')
    description=$(printf '%s' "$front" | sed -n 's/^description: *//p')
    if [ "$declared" = "$name" ]; then
        ok "$name declares the name of its directory"
    else
        bad "$name declares the name of its directory" "frontmatter says [$declared]"
    fi
    case "$description" in
        "This skill should be used"*) ok "$name describes when it applies, in third person" ;;
        "") bad "$name has a description" ;;
        *) bad "$name describes when it applies, in third person" "starts: ${description:0:40}" ;;
    esac
done

fresh_home
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --skills 2>&1)
missing=""
for skill in "$SKILLS_DIR"/*/; do
    name=$(basename "$skill")
    [ -L "$HOME_DIR/.claude/skills/$name" ] || missing="$missing $name"
done
if [ -z "$missing" ]; then
    ok "--skills links every skill into ~/.claude/skills"
else
    bad "--skills links every skill into ~/.claude/skills" "missing:$missing"
fi

# Codex reads the same format from its own directory, but only when it is
# installed -- a host without codex must not grow an empty ~/.codex.
fresh_home
mkdir -p "$HOME_DIR/.codex"
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --skills 2>&1)
if [ -L "$HOME_DIR/.codex/skills/lustre-ci-triage" ]; then
    ok "skills are linked for codex when codex is installed"
else
    bad "skills are linked for codex when codex is installed" "$out"
fi

fresh_home
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --skills 2>&1)
if [ -e "$HOME_DIR/.codex" ]; then
    bad "no ~/.codex is created for a host without codex"
else
    ok "no ~/.codex is created for a host without codex"
fi

# A skill directory someone wrote themselves must not be replaced by a link.
rm -f "$HOME_DIR/.claude/skills/lustre-ci-triage"
mkdir -p "$HOME_DIR/.claude/skills/lustre-ci-triage"
echo "mine" > "$HOME_DIR/.claude/skills/lustre-ci-triage/SKILL.md"
out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --skills 2>&1)
check "a real skill directory is left alone" "mine" \
    "$(cat "$HOME_DIR/.claude/skills/lustre-ci-triage/SKILL.md")"
contains "and the collision is reported" "not linked" "$out"

# Uninstalling removes the links it made, and nothing else.
ln -sfn /tmp "$HOME_DIR/.claude/skills/someone-elses"
HOME="$HOME_DIR" bash -c \
    "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; uninstall_skills" > /dev/null 2>&1
if [ -L "$HOME_DIR/.claude/skills/gerrit-patch-workflow" ]; then
    bad "uninstall removes the skill links"
else
    ok "uninstall removes the skill links"
fi
if [ -L "$HOME_DIR/.claude/skills/someone-elses" ]; then
    ok "uninstall leaves unrelated links alone"
else
    bad "uninstall leaves unrelated links alone"
fi
if [ -d "$HOME_DIR/.claude/skills/lustre-ci-triage" ]; then
    ok "uninstall leaves a real skill directory alone"
else
    bad "uninstall leaves a real skill directory alone"
fi

# --- a tool whose credentials are optional ---------------------------------
# jenkins reads are served anonymously, so the walkthrough must not push
# someone without an account into configuring it, and not configuring it
# is a normal outcome rather than a failure.
fresh_home
out=$(printf '\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jenkins'" 2>&1)
contains "an optional tool says the tool works without it" \
    "Optional -- the tool works without it" "$out"
contains "  and does not default to yes" "(not needed for reads) [y/N]" "$out"
contains "  and Enter leaves it read-only" "Left read-only" "$out"
# jenkins needs no server written: its URL has a built-in default.
if [ -s "$HOME_DIR/.config/jenkins-tool/.env" ] &&
    grep -q "JENKINS_TOKEN" "$HOME_DIR/.config/jenkins-tool/.env"; then
    bad "  no credential is written when declined"
else
    ok "  no credential is written when declined"
fi

out=$(HOME="$HOME_DIR" bash "$INSTALL_SH" --status 2>&1)
contains "--status calls an unconfigured optional tool workable" \
    "reads work" "$out"

# Saying yes and then giving nothing is also not a failure.
fresh_home
out=$(printf 'y\n\n\n\n' | env HOME="$HOME_DIR" VERIFY=0 \
    bash -c "INSTALL_SH_NO_MAIN=1 source '$INSTALL_SH'; configure_tools 'jenkins'" 2>&1)
contains "an unanswered optional credential leaves it read-only" \
    "leaving Jenkins read-only" "$out"

# --- the version-bump hook -------------------------------------------------
# The list this hook used to carry had drifted: it named crash_tool, which
# has no pyproject.toml, and not lreview, lustre_crash or gerrit_dashboard,
# whose versions therefore never moved.  Discovery cannot drift, and these
# pin the behaviour.
HOOK="$SCRIPT_DIR/.githooks/pre-commit"

hook_repo() {  # a checkout shaped like this one
    local repo
    repo=$(mktemp -d)
    TRASH="$TRASH $repo"
    (
        cd "$repo" || exit 1
        git init -q .
        git config user.email t@example.invalid
        git config user.name Test
        for tool in jira_tool lreview; do
            mkdir -p "$tool"
            printf '[project]\nname = "%s"\nversion = "0.2.0"\n' "$tool" \
                > "$tool/pyproject.toml"
            echo "x = 1" > "$tool/code.py"
        done
        mkdir docs && echo hi > docs/readme.md
        git add -A && git commit -qm init
        cp "$HOOK" .git/hooks/pre-commit
    )
    printf '%s' "$repo"
}

tool_version() {  # tool_version <repo> <tool>
    sed -n 's/^version = "\(.*\)"/\1/p' "$1/$2/pyproject.toml"
}

repo=$(hook_repo)
(cd "$repo" && echo "x = 2" > lreview/code.py && git add lreview/code.py &&
    git commit -qm "lreview change") > /dev/null 2>&1
check "a staged tool change bumps that tool" "0.2.1" "$(tool_version "$repo" lreview)"
check "  and leaves the other tools alone" "0.2.0" "$(tool_version "$repo" jira_tool)"

repo=$(hook_repo)
(cd "$repo" && echo bye > docs/readme.md && git add docs &&
    git commit -qm docs) > /dev/null 2>&1
check "a change outside every tool bumps nothing" "0.2.0" \
    "$(tool_version "$repo" lreview)"

repo=$(hook_repo)
(cd "$repo" &&
    printf '[project]\nname = "jira_tool"\nversion = "0.3.0"\n' \
        > jira_tool/pyproject.toml &&
    echo "x = 3" > jira_tool/code.py && git add -A &&
    git commit -qm "minor bump") > /dev/null 2>&1
check "a hand-edited version is not bumped on top" "0.3.0" \
    "$(tool_version "$repo" jira_tool)"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
