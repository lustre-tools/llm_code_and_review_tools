# Patch Watcher

Patch Watcher watches Gerrit changes over time and provides a deliberately
bounded engineering-control surface. It presents current review and CI state,
persists decisions and action history, and recommends the next human action.
Its first executable capability is one deterministic Maloo retest for a
revision whose enforced failures all have accepted Jira evidence. It does not
vote, post comments, modify source, upload patchsets, or invoke Claude for that
mechanical flow. Every patch starts Disabled and automatic external execution
also starts globally Disabled.

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
GERRIT_UPLOAD_ENABLED=false
# Required only when the separate upload kill switch is enabled:
GERRIT_GIT_NAME=Your Name
GERRIT_GIT_EMAIL=you@example.com
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
**Refresh all** updates the full list, and the heading shows the overall
last-checked time. A service-owned background observer performs the same
bounded polling at the configured interval, so observation continues when no
browser is open. Concurrent manual and scheduled polls coalesce.

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

The session foundation persists profiles, states, runner handles, recent
messages, timeout calculations, two-hour reminder delivery, and confirmed
cancel/kill actions. The page groups owner-matched LTVM VMs under active
sessions and shows other VMs separately. Guidance, waiting-human answers,
pause, interrupt, resume, follow-up, cancel, and kill operations are delivered
through the managed runner and recorded as durable, exactly-once actions.

The next dashboard card is **Worker admission and provenance**. It shows the
selected worker profile and hash, whether its execution boundaries are merely
declared or have been attested, the resolved tool inventory, warnings, and the
exact redacted reason for a blocked preflight. The checked-in compatibility
profile is deliberately labeled **Unsandboxed host worker**; it grants only
manual, read-only investigation capabilities and does not grant LTVM creation
or external writes.

Worker inputs are strict versioned JSON contracts. The controller first
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
private session database only after audit redaction.

Phase 0C adds a manual **Investigate** action to each current patch revision.
It checks out the exact Gerrit revision into a private run directory, admits
the declared worker environment, and starts a reconnectable Claude process
with only local source/evidence read tools. The run page exposes its durable
timeline, recent output, waiting-human question, and operator guidance and
stop controls. Destructive controls require a one-time POST confirmation;
links and GET requests cannot mutate a run. Phase 0C grants no Gerrit, CI,
Jira, LTVM, source-editing, shell, or upload capability.

Phase 1 adds deterministic Maloo test-error handling without a Claude
session. Each exact patch revision has one of four explicit policies:

- **Disabled**: apply the Gerrit gates and record why no flow ran;
- **Advise**: show the exact session-level retest that would be requested;
- **Approval**: prepare one exact action and wait for a separate operator
  confirmation tied to the revision and policy snapshot; or
- **Automatic**: execute only when the independently confirmed global gate is
  also enabled.

The controller checks the non-Maloo Code-Review `-1` gate before querying
Maloo, groups enforced failures by Maloo session, requires accepted Jira
evidence for every failed suite in that session, detects pending requests,
enforces a per-revision budget, and revalidates Gerrit and remote Maloo state
at the final write boundary. The durable outbox prevents duplicate requests
across repeated polls, concurrent controllers, and restarts. An uncertain
mutation is never blindly retried; later polls only reconcile remote state.
Outcomes and errors appear in the bounded timeline, daily report, and optional
immediate sendmail notices.

Phase 2 adds bounded Claude research for enforced Maloo failures that do not
have accepted Jira evidence. Its policy is independent from retest authority:
Disabled, Manual, or Automatic, with a maximum of 20 runs per exact revision.
Automatic starts also require the global execution switch. Every run receives
an immutable normalized evidence bundle, a pinned source checkout, and only
Read/Glob/Grep capabilities. It must return one of five classifications with
citations to captured evidence; malformed or invented citations fail closed.

The dashboard can then prepare a two-step operator-approved write workflow
for an existing Jira key. First, associate that key with the exact currently
observed failed Maloo suite. After Maloo reports the association accepted,
Patch Watcher prepares a separate approval for one session-level retest. Each
step has its own signed confirmation and revision revalidation. Planning does
nothing remotely, approvals are consumed by the background controller, and
ambiguous outcomes are never resubmitted blindly. Patch Watcher still cannot
create Jira issues, post Gerrit comments, edit source, or upload patchsets.

Maloo reads and retests use the installed `maloo` CLI. Configure that tool's
private credentials in `~/.config/maloo-tool/.env` as documented by
`maloo_tool`; Patch Watcher does not copy credentials into its database or
logs. Missing credentials are reported as a definitive authentication error
and cannot produce an ambiguous or retried mutation.

The automation ledger is private WAL-backed SQLite state:

```text
~/.local/state/patch-watcher/automation.sqlite3
```

Automatic policy changes and the global execution switch each use a separate
confirmation page. GET requests never enable or approve an external action.

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

Each patch has one compact **Actions** disclosure. It groups build failures,
test failures, and review comments; only implemented controls are interactive.
Build/review actions remain clearly marked as planned, while the current
test-failure policies retain their working controls.

## Controlled engineering runs (Phases 3A–3C)

An exact, refreshed patch revision can be prepared and then explicitly
confirmed for a controlled engineering run. Phase 3A creates a dedicated full
clone (not a Git worktree), lets Claude edit source files with no host shell or
service credentials, and independently captures the actual Git diff/status.

Phase 3B grants that confirmed run an open-ended command capability **inside
only its exactly owner-matched LTVM guests**. Claude may create an appropriate
VM or cluster, copy the pinned checkout into it, and run arbitrary diagnostic,
build, and test commands there. The broker revalidates ownership before every
guest command, bounds execution and captured output, and writes immutable
command/result audit records. This is one run-level capability grant, not a
per-command approval or an argv allowlist. It does not grant a host shell,
access to another run's VMs, or Gerrit writes.

The dashboard shows checkout ownership, session messages, capability and
attempt state, guest command results, resource exhaustion/cooldown state,
cleanup, artifacts, and exactly owner-matched LTVM inventory.

Phase 3C is a separate controller-owned Gerrit upload path. It is disabled by
default. A successful engineering run becomes eligible only when it has one
nonempty immutable diff and successful guest validation explicitly tagged as
test evidence. Preparing an upload rebuilds the exact pinned revision in a
fresh private staging checkout, verifies the diff, preserves the Gerrit
Change-Id, and records the proposed commit SHA. A second page shows the exact
old patchset/revision, diff digest, test-evidence digest, and proposed commit;
only its one-use POST can push. The controller rechecks Gerrit immediately
before dispatch and reconciles the proposed commit against all Gerrit
revisions after either success or an uncertain result. It never blindly
retries an ambiguous push. Claude never receives Gerrit credentials.

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

Use another local port with `python3 app.py --port 8090`, or select isolated
databases with `--session-database /private/path/sessions.sqlite3` and
`--automation-database /private/path/automation.sqlite3`.

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
checks, observed changes, deterministic retest events, current states, and
recent errors. With email
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
