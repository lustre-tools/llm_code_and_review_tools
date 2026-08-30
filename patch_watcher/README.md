# Patch Watcher

Patch Watcher is a small, read-only web application for watching Gerrit
changes over time. It presents the current watch, review, and CI state,
keeps a bounded in-process history, and recommends the next *human* action.
It does not vote, post comments, trigger tests, or otherwise modify Gerrit.

The status rules intentionally follow Marc Vef's Gerrit graph implementation:

- Gerrit lifecycle, current patchset, WIP flag, and timestamps
- Code-Review votes and unresolved-comment count
- Jenkins and Maloo Verified votes and current-patchset result links
- `Ready` only after both Jenkins and Maloo pass and enough non-owner
  Code-Review votes exist (two for native changes, one for backports)
- specific veto, Jenkins, Maloo, and other Verified failure states
- a top-level review gate: a non-Maloo Code-Review `-1` records the reviewer,
  patchset, and message, marks the patch for human attention, and prevents any
  future test-query stage; a Maloo Verified `-1` remains a CI failure signal

Patch Watcher's explicit watch state (`awaiting-ci`, `needs-review`,
`needs-attention`, `ci-failed`, `ready`, or `terminal`) is an extension point
inspired by Patch Shepherd. Recommendations are display-only; guarded actions
may be added later.

## Private configuration

Patch Watcher reads only this private user file; it does not read credentials
from environment variables or repository files:

```text
~/.config/patch-watcher/config
```

The file must have mode `0600` and contain:

```ini
GERRIT_URL=https://review.whamcloud.com
GERRIT_USER=your-gerrit-user
GERRIT_PASS=your-gerrit-http-password

# Optional; defaults shown
REFRESH_INTERVAL_SECONDS=300
EMAIL_ENABLED=false
EMAIL_TO=paf@mulberrytree.us
SENDMAIL_PATH=/usr/sbin/sendmail
```

Generate the HTTP password in Gerrit under **Settings → HTTP Credentials**.
Never add the private configuration to this repository. Email remains a dry
run until `EMAIL_ENABLED=true` is explicitly configured.

## Run locally

```bash
cd ~/llm_code_and_review_tools/patch_watcher
python3 app.py
```

Open <http://127.0.0.1:8080>. The server binds only to localhost. Adding a
patch requires only its Gerrit URL; the title comes from Gerrit, with the
change number as a temporary fallback. Adding performs a read-only refresh.
**Refresh all** updates the full list, and
the heading shows the overall last-checked time. While the page is open, the
browser visits a read-only refresh endpoint at the configured interval; no
background thread is required.

The top of the page shows a live worker-host memory summary and the current
LTVM inventory. Physical used/available memory, configured VM guest memory,
and measured QEMU RSS are deliberately separate figures. Running VM RSS is
verified against both the QEMU process and exact VM name before attribution;
stopped, legacy, or unowned VMs remain visible without becoming cleanup
targets. Resource collection is cached for 15 seconds and can be refreshed
explicitly from the page.

Managed-session state is stored in the private SQLite database:

```text
~/.local/state/patch-watcher/sessions.sqlite3
```

The session foundation persists profiles, states, PIDs, recent messages,
timeout calculations, two-hour reminder delivery, and confirmed cancel/kill
intents. The page groups owner-matched LTVM VMs under active sessions and shows
other VMs separately. Guidance and kill controls currently record durable
controller intent; process signalling and Claude message delivery arrive with
the managed runner phase and are not falsely reported as completed actions.

The next dashboard card is **Worker admission and provenance**. It shows the
selected worker profile and hash, whether its execution boundaries are merely
declared or have been attested, the resolved tool inventory, warnings, and the
exact redacted reason for a blocked preflight. The checked-in compatibility
profile is deliberately labeled **Unsandboxed host worker**; it grants only
manual, read-only investigation capabilities and does not grant LTVM creation
or external writes.

Phase 0B worker inputs are strict versioned JSON contracts. A controller first
creates a private per-run directory and revision-pinned run envelope, then
runs the dependency-free doctor before starting Claude:

```bash
cd ~/llm_code_and_review_tools/patch_watcher
python3 pw_worker.py doctor \
  --profile host-unsandboxed-mac-v1 \
  --run-envelope /private/run/path/run-envelope.json \
  --json
```

Exit status `0` means the environment is admitted as `ready` or `degraded`;
exit status `1` means it is `blocked`. The JSON result is suitable for the
private session database only after audit redaction. The current web service
displays persisted admission evidence but still does not start Claude. The
manual read-only runner and live operator messaging are the following Phase
0C slice.

The page shows clickable Gerrit and leading Jira-ticket links, current and
historical status, last-checked and Gerrit last-changed times, and a short
description of the newest upload or message. Refresh failures preserve the
last known state and are written as private structured JSONL records under
`~/.local/state/patch-watcher/errors.jsonl`.

Review health, CI, WIP, and watch states use the same green/red/amber/blue
visual vocabulary as the Gerrit graph. Every colored chip also
contains explicit text and a symbol, so meaning never depends on color alone.
Review health is summarized as **Ready**, **Clean**, **Needs**, or **Veto**;
Jenkins and Maloo retain their explicit pass/fail/running labels.
The lifecycle remains in the status model, but the table folds terminal
lifecycle into watch state: merged patches display **Merged** and abandoned
patches display **Abandoned** rather than occupying a separate column. Jenkins
and Maloo chips appear inside **Watch state / CI**. Patchset appears as compact
`PS N` metadata under the patch title; only actual work-in-progress changes
show a WIP badge, so there is no ambiguous “Active” label.

The **Handle reviews** section shows two disabled design stubs: handling only
simple comments, or asking Claude Code to attempt all comments. Neither option
is selectable, invokes Claude, sends escalation email, or writes to Gerrit.
Their proposed escalation behavior is documented in `DESIGN_ACTION_FLOW.md`.

## Design documents

- `DESIGN_ACTION_FLOW.md` defines the product-policy flow for test failures,
  Jenkins failures, and future review handling.
- `AGENT_ORCHESTRATION_DESIGN.md` defines the implementation architecture,
  durable state machines, native Claude runner, human messaging,
  LTVM/resource lifecycle, isolation roadmap, dashboard, recovery behavior,
  and phased acceptance criteria.
- `WORKER_ENVIRONMENT_CONTRACT.md` defines what lives in the portable AI
  engineer environment, what the controller injects, how a worker is admitted,
  and how the current Mac evolves into reproducible and isolated workers.

Use another local port with `python3 app.py --port 8090`, or select an isolated
session database with `--session-database /private/path/sessions.sqlite3`.

## Seed a watch list

The durable watch-list file is `~/.config/patch-watcher/patches.txt`. Adding or
removing a patch updates it atomically with private `0600` permissions, and a
restart reloads the same watch list. Each
non-comment line is a Gerrit URL followed by an optional tab-separated title:

```text
https://review.whamcloud.com/c/fs/lustre-release/+/61965
https://review.whamcloud.com/c/fs/lustre-release/+/61966	Optional temporary title
```

Gerrit replaces temporary titles during refresh. Select another file with
`python3 app.py --seed-file /path/to/patches.txt`.

## Daily email summary

The **Send status email** button composes a bounded plain-text summary of
checks, observed changes, current states, and recent errors. With email
disabled it reports a dry run and never invokes sendmail. When explicitly
enabled, Patch Watcher submits an RFC-822 message to the configured Linux
sendmail binary using `sendmail -t -oi`; it never invokes a shell.

For an external daily scheduler, run:

```bash
cd ~/llm_code_and_review_tools/patch_watcher
python3 app.py --daily-summary
```

This loads and refreshes the seed list before composing the summary. Schedule
that command with the host's normal cron or systemd timer rather than keeping
scheduling logic inside the web process.

## Tests

All Gerrit and sendmail behavior is mocked; the test suite performs no network
requests and sends no email:

```bash
python3 -m unittest discover -s . -v
```
