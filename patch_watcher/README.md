# Patch Watcher

Patch Watcher watches Gerrit changes over time and provides a deliberately
bounded engineering-control surface. It presents current review and CI state,
persists decisions and action history, and recommends the next human action.
It now supports deterministic Maloo retests, bounded research, controlled
engineering/review/build-repair runs, and exact controller-owned patchset,
review-reply, and Jenkins-retrigger writes. Workers never receive Gerrit or
Jenkins credentials. Every patch starts with all standing actions off,
automatic triggering starts off, and every remote-write kill switch starts
off.

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
inspired by Patch Shepherd. Guarded actions are exposed separately from status,
with exact-state bindings, durable history, bounded authority, and explicit
kill switches.

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
GERRIT_REPLY_ENABLED=false
JENKINS_RETRIGGER_ENABLED=false
# Required only when the separate upload kill switch is enabled:
GERRIT_GIT_NAME=Your Name
GERRIT_GIT_EMAIL=you@example.com
```

Generate the HTTP password in Gerrit under **Settings → HTTP Credentials**.
Never add the private configuration to this repository. Email remains a dry
run until `EMAIL_ENABLED=true` is explicitly configured.

Gerrit patchset upload and reply posting reuse the Gerrit credentials above,
but have independent kill switches. Jenkins retrigger uses the existing
`jenkins_tool` private configuration at `~/.config/jenkins-tool/.env` and is
available only when `JENKINS_RETRIGGER_ENABLED=true`. Enabling one write type
does not enable another.

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
session. The compact standing-policy form persists four independent
per-patch choices: trigger mode (`manual` or `automatic`), test failures
(`off`, `deterministic`, or `investigate`), build failures (`off` or `repair`),
and review comments (`off`, `simple`, or `all`). Automatic triggering also
requires the separately confirmed global execution gate. Exact-revision
fingerprints coalesce duplicate observations and one patch cannot acquire a
second active managed run.

For test failures, `manual` maps deterministic actions to approval and unknown
failures to manually started research; `automatic` permits those exact actions
only when the independently confirmed global gate is also enabled. The dry-run
view remains available for inspecting the deterministic decision.

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

Standing policy is stored atomically with mode `0600` in
`~/.config/patch-watcher/standing-policies.json`. Gerrit reply and Jenkins
retrigger claims have separate private SQLite ledgers so restarts cannot erase
or duplicate a claimed external write.

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
The current test-failure policies retain their working controls. Review and
Jenkins build-failure runs may be started manually. They may also start from
standing policy, but only after the operator explicitly confirms that patch's
automatic policy and independently enables the global automatic-execution
gate. In both cases the run is bound to captured exact-revision inputs, and
the separate upload capability must be enabled for publication.

## Jenkins build-failure repair

For a completed failed Jenkins build attached to the exact current Gerrit
revision, **Handle build failure** captures an immutable job, build, revision,
and bounded log snapshot. One confirmation starts a dedicated full checkout
and also preauthorizes one controller-owned patchset upload if, and only if,
the worker reports `patch_caused_fixed`, produces a nonempty diff, and supplies
successful explicitly tagged build and test evidence. There is no second
upload confirmation.

The worker has no Gerrit or Jenkins credentials. Its command capability is
open-ended only inside LTVM guests carrying that exact run's owner ID. Stale or
changed inputs, infrastructure/transient/unrelated/ambiguous diagnoses,
no-diff or validation failures, resource exhaustion, and publication trouble
all stop for a human. Upload preparation and dispatch use the durable,
controller-owned writer with one idempotency binding over the run, change,
patchset, revision, diff, and validation evidence. A claimed or ambiguous push
is reconciled against Gerrit, including during periodic restart recovery, and
is never blindly repeated. A successful upload is refreshed as a new patchset,
making the completed run stale. The separate **Retrigger Jenkins** action can
retrigger the exact failed build when its independent kill switch is enabled;
it does not grant the repair worker Jenkins credentials. The exact failed
build is a terminal one-use action identity: after dispatch is claimed, a
failed or ambiguous response permits reconciliation only, never another blind
dispatch. A genuinely new failed build is a new identity. Abort,
configuration, and other Jenkins writes remain future work.

## Review handling and replies

**Handle simple comments** and **Handle all comments** capture one immutable
unresolved-comment snapshot and start an exact-revision engineering run. The
single run-start confirmation preauthorizes exactly one qualifying patchset
upload after a nonempty diff and successful LTVM test evidence; it does not
ask for a second upload approval. Standing automatic starts require their own
explicit policy confirmation and the global automatic-execution gate.

Reply posting is a separate Phase 5B controller write, independently disabled
by default. A reply intentionally targets the original revision and exact
comment/location from the run snapshot, even when the handler has since
uploaded a newer patchset. Immediately before posting, the controller verifies
that historical revision, comment identity, file/line/range, and unresolved
state rather than incorrectly rebinding the reply to the new current revision.
The immutable reply plan is one-use and reconciliation-only after an uncertain
dispatch. Claude never receives Gerrit credentials.

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
only its one-use POST can push. Review-handling and build-repair runs instead
use their already confirmed run-start grant for exactly one qualifying upload,
with no second approval. The controller rechecks Gerrit immediately
before dispatch and reconciles the proposed commit against all Gerrit
revisions after either success or an uncertain result. It never blindly
retries an ambiguous push. Claude never receives Gerrit credentials.

## Autonomous lanes (Phases 6A–6B)

The dashboard now exposes a code-defined, versioned autonomous-lane framework
with separate global, project, and patch kill switches. Every switch starts
off. Enabling a scope requires a signed one-use confirmation; disabling it is
immediate. Controls are stored privately in
`~/.config/patch-watcher/autonomous-lanes.json`, while exact decisions are
written to the append-only
`~/.local/state/patch-watcher/autonomous-lanes.jsonl` audit.

The first lane is `deterministic-test-retest` version 1. It wraps the existing
Maloo retest evaluator and durable outbox rather than adding a second executor.
An enrolled patch still requires its confirmed automatic/deterministic
standing policy and the primary global automation gate. The lane permits at
most one Maloo retest write per exact revision and grants zero Claude runs and
no Gerrit, Jenkins, Jira, checkout, or LTVM authority. All controls and the
fixed lane definition are checked again immediately before the remote write.
Patches not enrolled in a lane retain their existing behavior.

The dashboard explains every admission or rejection, shows budgets and recent
outcomes, and can replay all historical decisions through the pure evaluator.
Replay does not create observations, triggers, runs, actions, or remote writes.

## Design documents

- `DESIGN_ACTION_FLOW.md` defines the product-policy flow for test failures,
  Jenkins build-failure repair, and review handling.
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
