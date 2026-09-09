# Patch Watcher

Patch Watcher watches Gerrit changes over time and provides a deliberately
bounded engineering-control surface. It presents current review and CI state,
persists decisions and action history, and recommends the next human action.
It supports deterministic Maloo retests, bounded research, and
engineering/review/build-repair agent sessions. Every patch starts with all
standing actions off and automatic triggering off.

> **Status: mid-redesign.** The agent is no longer treated as an untrusted
> credential-free worker; it runs in the same environment a developer does and
> performs its own Gerrit, Maloo, and Jenkins writes. The worker-admission and
> controller-owned-write layers have been removed. See
> `~/lustre_design_docs/plans/agent-orchestration/PLAN.md` for the target
> architecture and remaining phases.

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

## Install and set up a host

Patch Watcher runs agents in the same environment a developer has on this box:
a host shell, the installed LLM tools, `ltvm`, and a Lustre checkout. Setting a
host up is therefore ordinary installation, not sandbox attestation.

```bash
cd ~/llm_code_and_review_tools
./install.sh --with-ltvm     # tools + ltvm from lustre-test-vms-v2
./install.sh --configure     # interactive credential setup
./install.sh --doctor        # can this host run agents?
```

`--configure` walks the five private credential files the tools already read
and prompts only for what is missing (`--reconfigure` prompts for everything).
Secrets are never echoed, unrelated keys in each file are preserved, and every
file is created mode `0600`:

```text
~/.config/gerrit-cli/.env
~/.config/jira-tool/.env
~/.config/jenkins-tool/.env
~/.config/maloo-tool/.env
~/.config/patch-watcher/config
```

Two things it cannot do for you:

1. **Accept the background-agent disclaimer.** An unattended agent cannot
   answer a permission prompt, so runs use `bypassPermissions`, and the CLI
   refuses that in background mode until a human accepts once:
   ```bash
   claude --dangerously-skip-permissions    # once, interactively
   ```
2. **Declare the checkout pool.** Agents reuse numbered Lustre checkouts rather
   than cloning per run, and the checkout index becomes the run's VM name
   prefix (`co<N>-<role>`), which is how VMs are attributed and cleaned up.

   ```text
   ~/.config/patch-watcher/checkout-pool.json
   {"root": "/home/you/lustre_checkouts/master_checkouts", "checkouts": [1, 2, 3]}
   ```

   **Do not list a checkout you work in yourself.** An agent resets and cleans
   its checkout before every run. An undeclared pool is not fatal -- runs fall
   back to a private per-run clone and get no reserved VM prefix -- so the
   default is empty rather than "every directory that looks like a checkout".

`pw-doctor` reports blocking problems separately from advisory ones and exits
non-zero only for the blocking kind.

## Run locally

```bash
patch-watcher
```

`patch-watcher` is installed by `./install.sh`. Without installing, run it as
a module from the directory that *contains* the package -- this directory, not
the `llm_code_and_review_tools` root, where `python3 -m patch_watcher.app`
reports `No module named patch_watcher.app`:

```bash
cd ~/llm_code_and_review_tools/patch_watcher
python3 -m patch_watcher.app
```

(`python3 patch_watcher/app.py` does not work either -- the modules import each
other through the `patch_watcher` package.)

Open <http://127.0.0.1:8080>. The server answers only to `127.0.0.1` and
`localhost` by name, so a page that rebinds DNS to the loopback address cannot
read the dashboard. The server binds only to localhost. Adding a
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

Each current patch revision has a manual **Investigate** action. It checks out
the exact Gerrit revision into a private run directory and starts a
reconnectable Claude session against it. The run page exposes its durable
timeline, recent output, waiting-human question, and operator guidance and
stop controls. Destructive controls require a one-time POST confirmation;
links and GET requests cannot mutate a run.

Deterministic Maloo test-error handling without a Claude
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

Bounded Claude research for enforced Maloo failures that do not
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
`~/.config/patch-watcher/standing-policies.json`.

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
show a WIP badge, so there is no ambiguous "Active" label.

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
and bounded log snapshot, then starts a dedicated full checkout and an agent
session against it. The agent investigates and, if it produces a fix, pushes
the patchset itself using the installed `gerrit` CLI.


## Review handling

**Handle simple comments** and **Handle all comments** capture one immutable
unresolved-comment snapshot and start an exact-revision agent session. The
agent addresses the comments, and posts replies and any new patchset itself
using the installed `gerrit` CLI.


## Engineering runs

An exact, refreshed patch revision can be prepared and then confirmed for an
engineering run. The controller creates a dedicated full clone, pins it to the
revision, and starts a reconnectable Claude session in it. The agent has the
same environment a developer has on this host: `ltvm`, the LLM tools, and a
Lustre checkout. It may create VMs, build, and test.

The dashboard shows checkout ownership, session messages, guest command
results, resource state, cleanup, artifacts, and LTVM inventory.

### Model and reasoning effort

The start controls carry a model field and a reasoning-effort selector, both
optional; blank means "whatever this Patch Watcher is configured with". The
choice is covered by the start confirmation's signature, so the page that
shows you a model cannot start a run with a different one, and it is stored on
the run -- the run page reports what that run actually used, not what the
current default happens to be.

Process-wide defaults come from `--model` and `--effort`:

```bash
patch-watcher --model claude-opus-5 --effort high
```

Effort accepts `low`, `medium`, `high`, `xhigh` and `max`. A model name is
validated against a conservative pattern rather than escaped, because it
becomes an argument to the `claude` process.

### What the agent knows about this host

The run prompt carries the task, the paths, the prohibitions, the time budget
and the report contract -- it does **not** carry `ltvm` syntax, build
recipes, or the `co<N>-<role>` naming convention. All of that reaches the
agent the same way it reaches you: Claude Code discovers `CLAUDE.md` by
walking up from its working directory, which is the checkout.

That means the pool checkouts must sit under a directory that has a
`CLAUDE.md` above them. If `$CO` points somewhere else, agents still run and
still have credentials -- they just no longer know how to build or test
anything here. `pw-doctor` reports this as `pool:agent-instructions`.


## Autonomous lanes

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
- `~/lustre_design_docs/plans/agent-orchestration/PLAN.md` defines the
  in-progress redesign: what the tool is becoming, what is being deleted, and
  the phase order.

`AGENT_ORCHESTRATION_DESIGN.md` and `DESIGN_ACTION_FLOW.md` still describe the
credential-free-worker architecture in places and are superseded by `PLAN.md`
where they disagree.

Use another local port with `patch-watcher --port 8090`, or select isolated
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
`patch-watcher --seed-file /path/to/patches.txt`.

## Daily email summary

The **Send status email** button composes a bounded plain-text summary of
checks, observed changes, deterministic retest events, current states, and
recent errors. With email
disabled it reports a dry run and never invokes sendmail. When explicitly
enabled, Patch Watcher submits an RFC-822 message to the configured Linux
sendmail binary using `sendmail -t -oi`; it never invokes a shell.

For an external daily scheduler, run:

```bash
patch-watcher --daily-summary
```

This loads and refreshes the seed list before composing the summary. Schedule
that command with the host's normal cron or systemd timer rather than keeping
scheduling logic inside the web process.

## Tests

The unit suite is hermetic: no network, no credentials, no email, no browser.
Run it from this directory -- discovery from the repository root finds nothing:

```bash
cd ~/llm_code_and_review_tools/patch_watcher
python3 -m unittest discover -s . -v
```

Two further checks are opt-in because they need credentials and network. Both
are read-only against Gerrit -- they watch deliberately dormant changes and
never post, vote, upload, or start an agent:

```bash
cd ~/llm_code_and_review_tools/patch_watcher

# End-to-end: fetch real changes, render the dashboard.
PATCH_WATCHER_TEST_CONFIG=~/.config/patch-watcher/config \
    python3 integration_check.py

# The same, driven in Chrome: rendering, controls, accessibility, and the
# GET-never-mutates and CSRF invariants.
PATCH_WATCHER_TEST_CONFIG=~/.config/patch-watcher/config make browser
```

`make browser` runs `browser_check.py` with `~/llm_code_and_review_tools/.venv`
if that venv exists -- playwright usually lives there rather than in the system
Python -- and with `python3` otherwise. That venv is not guaranteed: install.sh
creates it only on PEP 668 hosts or with `--venv`, and `--venv PATH` puts it
elsewhere. To pick the interpreter yourself:
`VENV_PYTHON=/path/to/python make browser`.

The browser check needs `playwright` (`pip install playwright`) and uses the
system Chrome. It exists because unit tests could not catch what it caught: a
missing CSRF check on the watch-list routes, a state label rendered as
"Ci Failed", and a console error on every page load.
