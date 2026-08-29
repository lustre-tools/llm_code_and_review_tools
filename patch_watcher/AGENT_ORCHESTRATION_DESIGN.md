# Patch Watcher Agent Orchestration Design

This document turns the Patch Watcher action flow into an implementation
contract. `DESIGN_ACTION_FLOW.md` remains the product-policy description: it
defines when test, build, and review conditions matter. This document defines
how Patch Watcher observes those conditions, starts controlled work, exposes
that work to a person, and recovers safely.

The intended direction is gradual. Patch Watcher begins as an observable,
deterministic retest controller. It later gains read-only Claude Code research,
then isolated build and patch-editing work, and only eventually narrow lanes
that may complete without a human. A later phase also containerizes Claude
workers and supports restricted network access. Early read-only workers may run
unsandboxed, but the dashboard must say so explicitly.

## Goals

- Preserve the current useful patch-status dashboard while adding durable
  automation state.
- Make every trigger, decision, external action, agent message, and result
  inspectable from the dashboard.
- Give a human clear controls to start, message, pause, interrupt, resume,
  cancel, retry, or supersede work.
- Never let two workers act concurrently on the same Gerrit change.
- Pin work to an exact Gerrit patchset and revision SHA; stale work must never
  affect a newer patchset.
- Grant each run only the capabilities required for that run.
- Survive Patch Watcher, the Claude runner, Claude Code, and host restarts
  without losing the audit trail or repeating ambiguous external actions.
- Reuse the LLM review tools and LTVM rather than reimplementing Gerrit, JIRA,
  Maloo, Jenkins, checkout, build, or VM operations.
- Keep the deployment simple enough to run as one local service initially.

## Non-goals for the first executable phases

- A general distributed workflow engine.
- Multiple active agents on one patch.
- Automatic source modification, Gerrit comments, votes, or patch uploads.
- Running untrusted patch build/test commands directly on the Patch Watcher
  host.
- Treating free-form agent text as authorization for an external action.
- Treating a browser page load as the scheduler.

## Design review: gaps in the current prototype

The current prototype is intentionally small, but these gaps must be closed
before automation is enabled:

1. The watch list and history are process memory. A restart loses them.
2. Refresh is driven by a browser meta-refresh. Polling and automation must run
   even when no browser is open.
3. Patch observation, automation policy, trigger, agent run, Claude turn, and
   external action are not separate concepts yet. Combining them produces
   misleading states such as “idle but working” or “failed but waiting.”
4. There is no transaction that prevents duplicate runs or duplicate retest
   requests when two refreshes observe the same state.
5. There is no exact patchset/SHA pin or final stale-patchset check before a
   side effect.
6. Claude Voice Control has useful process/session patterns, but making its CLI
   and registry a Patch Watcher dependency would couple two different products.
7. `WORKER_STATUS` text markers are useful for compatibility, but they are too
   weak to be the only agent protocol.
8. Current checkboxes do not define trigger mode, capability scope, budgets,
   approval rules, or what a policy edit does to an active run.
9. LTVM work needs explicit VM and checkout ownership, cleanup, and artifact
   rules.
10. Gerrit comments, source, CI logs, and JIRA text are untrusted inputs. They
    can contain prompt-injection text and must never redefine worker authority.
11. Crash recovery and ambiguous external-call recovery are unspecified.
12. The dashboard does not yet have a run detail view, conversation, pending
    question, delivery state, or operator controls.

The phased plan below addresses these before progressively enabling more
powerful actions.

## Core invariants

These are implementation rules, not suggestions.

1. **One active run per Gerrit change.** The database enforces this. A second
   trigger is coalesced or supersedes the existing run through an explicit
   transition; it never silently starts another worker.
2. **Every run is revision-pinned.** It stores change number, patchset number,
   and revision SHA. Before every external write, Patch Watcher refreshes and
   compares all three.
3. **The database is authoritative.** Patch Watcher's Claude runner owns Claude
   process continuity; LTVM performs requested VM operations; neither decides
   Patch Watcher workflow state.
4. **Policy is snapshotted at run creation.** Editing patch policy affects the
   next run by default. The UI requires a separate explicit action to alter or
   cancel an active run.
5. **Side effects are controller-owned.** Workers request actions. The
   controller validates policy, revision, budget, and idempotency before an
   adapter performs them.
6. **No action is inferred from prose.** Only a validated structured request
   can advance state or request an external write.
7. **Every state change is an event.** Current-state columns are projections
   for efficient UI display; the event history remains append-only.
8. **Terminal means terminal.** A message to a completed, failed, cancelled,
   or stale run cannot silently revive it. The operator starts a follow-up run.
9. **Untrusted code runs away from the host.** Build and test execution belongs
   in LTVM or a later worker sandbox, never in the web-service process.
10. **Emergency stops are always available.** Global automation, per-patch
    automation, and an individual run can each be disabled independently.

## Vocabulary and separate state domains

The UI and code should use these terms consistently:

- **Patch:** a watched Gerrit change, independent of patchset.
- **Revision:** one observed Gerrit patchset and exact revision SHA.
- **Policy:** operator-selected triggers, capabilities, budgets, and approval
  rules for a patch.
- **Observation:** one read-only snapshot of Gerrit, review, Jenkins, Maloo,
  and related metadata.
- **Trigger:** a durable fact that might justify work, such as a newly observed
  enforced test failure or a manual request.
- **Run:** one bounded attempt to handle a trigger against one pinned revision.
- **Session:** the managed Claude conversation/process associated with an
  agent-backed run.
- **Turn:** one prompt/response cycle inside that session.
- **Action attempt:** one controller-mediated external operation, such as a
  Maloo retest request.
- **Session resource:** an ephemeral checkout, VM, or VM cluster created for and
  recorded against one agent session.

Patch status, run status, Claude turn status, and action status must be stored
and displayed separately. For example, a patch may be `ci-failed`, its run may
be `waiting_external`, and its Claude session may currently be `idle`.

## Initial architecture

Implement the boundaries below as Python modules, but deploy them as one
service plus subprocesses at first. This avoids premature microservices while
keeping later separation possible.

### Web UI and HTTP API

- Reads durable projections from the database.
- Validates operator commands and records them as commands/events.
- Never performs long-running Gerrit, agent, build, or VM work in a request.
- Uses CSRF protection and authenticated access before being exposed beyond
  localhost.

### Observer

- Polls watched changes on the configured schedule, independent of browsers.
- Calls read-only Gerrit, Jenkins, and Maloo adapters.
- Stores a normalized observation and a bounded reference to raw adapter data.
- Emits trigger candidates only when the normalized state meaningfully changes.

### Evaluator

- Applies `DESIGN_ACTION_FLOW.md` and the effective per-patch policy.
- Applies the non-Maloo Code-Review `-1` gate before the test flow.
- Suppresses, coalesces, or queues a trigger with a recorded reason.
- Does not call Claude for deterministic rules.

### Dispatcher and reconciler

- Claims the per-patch active-run slot transactionally.
- Creates runs and dispatches deterministic actions or agent work.
- Holds a renewable singleton dispatcher lock in the initial SQLite
  deployment, so accidentally starting two service processes cannot create two
  schedulers. A standby process may serve read-only UI traffic but may not
  dispatch until it owns that lock.
- Periodically reconciles database state with Claude runner sessions, action
  adapters, full checkouts, and session-created LTVM resources.
- Detects stalled, orphaned, externally completed, and stale work.

### Runner adapters

- **Deterministic runner:** executes bounded controller workflows such as one
  Maloo retest request without starting Claude.
- **Native Claude runner:** starts, resumes, messages, interrupts, stops, and
  reads structured event streams for Claude sessions. It borrows proven ideas
  from Claude Voice Control without depending on that application.
- **Checkout adapter:** creates and verifies full, independent,
  revision-pinned source checkouts.
- **Tracked LTVM tool:** lets the agent choose and create the target, topology,
  and VM parameters it needs while automatically registering every created VM
  against the current session and handling terminal cleanup.

### Tool adapters

Wrap `gc`, `maloo`, `jenkins`, `jira`, and later `janitor` using their JSON
output and documented exit codes. Each adapter returns a normalized typed
result while retaining a redacted raw-result reference for debugging. Do not
parse human-oriented terminal tables.

## Persistence model

Use SQLite initially, with foreign keys enabled, WAL mode, transactions, and
schema migrations. A single local deployment does not need PostgreSQL, but the
schema must not depend on Python process memory.

Minimum entities:

| Entity | Important fields |
| --- | --- |
| `patch` | id, Gerrit URL/change number/project, enabled, current lifecycle, created/updated |
| `patch_revision` | patch id, patchset, revision SHA, subject, owner, observed timestamps |
| `patch_policy` | version, triggers, capabilities, approvals, budgets, notification settings |
| `observation` | patch/revision, checked time, normalized review/CI state, source fingerprints |
| `trigger` | patch/revision, type, fingerprint, state, reason, first/last observed |
| `run` | patch/revision, trigger, policy snapshot, type, state, summary/question/error, timestamps/version |
| `run_event` | run, monotonic sequence, actor, type, structured payload, timestamp |
| `run_message` | run, author, body, urgency, delivery state, target question/turn, timestamps |
| `action_attempt` | run, action type, idempotency key, state, request/result, timestamps |
| `session_resource` | run/session, type, name, create request, environment, lifecycle state, last seen, cleanup result |
| `artifact` | run, kind, path/URI, content hash, size, description, retention state |
| `notification` | run/patch, type, destination, idempotency key, delivery state/result |
| `service_cursor` | observer/reconciler/Claude-log cursors and last successful activity |

Required constraints include:

- one patch per Gerrit server and change number;
- one revision per patchset/SHA;
- one trigger per stable fingerprint;
- one non-terminal run per patch (partial unique index);
- one action attempt per idempotency key;
- one live ownership record per checkout or VM name;
- ordered, unique event sequence numbers per run.

Large Claude logs, build logs, and VM artifacts stay in private files; the
database stores metadata, bounded excerpts, hashes, and paths. Secrets and raw
credentials never enter events or artifacts.

## State machines

### Trigger states

- `candidate`: observed but not evaluated.
- `queued`: eligible and awaiting a run.
- `coalesced`: represented by an existing active run.
- `suppressed`: policy or a safety gate says not to act; the reason is stored.
- `obsolete`: the patchset or external condition changed before dispatch.
- `dispatched`: attached to a run.

A stable fingerprint should include the change number, revision SHA, trigger
type, and triggering external identifiers (for example Maloo session/test
group). Repeated polling therefore updates `last_observed` instead of creating
new work.

### Run states

- `queued`: durable and eligible, but not allocated.
- `preparing`: starting the session and creating its full source checkout.
- `running`: controller or agent is actively performing a step.
- `waiting_external`: waiting for a known CI, timer, or other external result.
- `waiting_human`: paused on one explicit operator question or decision.
- `paused`: explicitly paused by an operator without an unanswered question.
- `blocked`: cannot proceed because of infrastructure, authentication, or an
  unmet dependency; operator intervention is required.
- `succeeded`: intended bounded outcome completed.
- `failed`: attempt ended unsuccessfully and is not automatically continuing.
- `cancelled`: operator stopped the run.
- `stale`: pinned Gerrit revision is no longer current.

All transitions use optimistic concurrency (`run.version`) and are recorded in
`run_event`. `waiting_human`, `waiting_external`, `paused`, and `blocked` keep
the logical one-active-run-per-patch claim. If a new patchset appears, the reconciler
marks the old run `stale`, releases mutable resources according to policy, and
allows evaluation of the new revision.

### Claude state mapping

The native runner should retain Claude Voice Control's useful distinction
between turn state and task state rather than collapsing them:

| Claude runner observation | Patch Watcher interpretation |
| --- | --- |
| turn `running` | session is active; run usually remains `running` |
| turn `waiting`/`idle`, task `in_progress` | turn ended; controller evaluates the structured report before continuing |
| task `needs_input` | validate question and move run to `waiting_human` |
| task `blocked` | move to `blocked`, unless a clear human question makes `waiting_human` more accurate |
| task `complete` | validate requested outcome and artifacts before `succeeded` |
| failed/stopped process | reconcile as `blocked`, `failed`, `paused`, or `cancelled` according to cause |

Claude process state alone never marks a run successful.

### Action-attempt states

- `planned`, `executing`, `succeeded`, `failed`, `ambiguous`, or `cancelled`.

The controller writes `planned` and its idempotency key before invoking a tool.
If it crashes during the call, startup reconciliation treats it as `ambiguous`
and checks remote state before any retry. This is an outbox-style contract:
exactly-once delivery cannot be assumed from an HTTP or CLI call.

For example, a Maloo retest key may be:

```text
maloo-retest:<change>:<revision-sha>:<session-id>:<test-group>
```

## Trigger and dispatch algorithm

1. Observer stores a new normalized revision snapshot.
2. Evaluator applies the review gate and patch policy.
3. It creates or updates one fingerprinted trigger.
4. In one database transaction, dispatcher checks:
   - global automation enabled;
   - patch automation enabled;
   - trigger mode permits this event;
   - no non-terminal run owns the patch;
   - current revision still matches the trigger;
   - retry, runtime, turn, and action budgets remain.
5. Dispatcher snapshots policy, creates the run, claims the active-run slot, and
   marks the trigger dispatched.
6. Runner executes one bounded step, records events/actions, and yields to the
   reconciler between steps.
7. Any newer revision immediately makes the run stale before another side
   effect can occur.

Manual triggers follow the same path. They do not bypass revision, ownership,
capability, budget, or idempotency checks.

## Policy and capabilities

The visible checkboxes are a friendly projection of a versioned policy, not
authorization by themselves. Each patch shows both inherited defaults and
effective overrides.

Policy fields should include:

- `automation_enabled`;
- trigger mode: `manual`, selected state changes, and/or schedule;
- enabled workflow categories: test, Jenkins build, simple review comments,
  all review comments;
- runner type and model/effort preference;
- capability grant;
- runtime, turn, retry, external-action, and optional cost budgets;
- stale-run behavior and timeout behavior;
- approval requirements;
- immediate and daily notification settings;
- worker isolation/network profile when implemented.

Each workflow capability has one of four operator-visible action modes:

- `disabled`: observe only; create no recommendation or run;
- `advise`: evaluate and display what would be done, but perform no action;
- `approval`: prepare the exact action and wait for an operator to approve it;
- `automatic`: execute when all policy and safety checks pass.

The initial checkboxes may map to these modes through a compact control, but a
checked box must never leave the effective mode ambiguous. New and migrated
patches default to `disabled`.

Capability names should be explicit and composable:

- `read_gerrit`, `read_ci`, `read_jira`, `read_repository`;
- `request_maloo_retest`;
- `create_checkout`, `edit_source`;
- `start_ltvm`, `run_vm_tests`;
- `post_gerrit_message`, `reply_review_comment`, `vote_gerrit`;
- `upload_patchset`;
- `network_general`.

Early agents receive read capabilities only. The controller retains Gerrit,
Maloo, Jenkins, JIRA, and VM credentials and performs approved writes through
adapters. Prompt text is defense in depth; enforcement belongs in the
controller, credentials, OS identity, sandbox, and tool proxy.

Dangerous capability changes show an explicit confirmation and apply to future
runs. A global emergency stop prevents new actions and pauses dispatch without
destroying evidence from existing runs.

## Exact revision and stale-work handling

At run creation, store:

- Gerrit server and change number;
- patchset number;
- revision commit SHA;
- subject/project/branch;
- relevant CI result identifiers;
- policy version and trigger fingerprint.

Before a retest, comment, vote, upload, or other external write, re-fetch the
current Gerrit revision and compare patchset and SHA. A mismatch:

1. records a `revision_changed` event;
2. prevents the action;
3. marks the run stale;
4. preserves its logs and artifacts;
5. cleans its checkout and session-created VMs, unless an operator has
   explicitly retained a resource for debugging; and
6. lets the new revision create its own trigger/run.

An operator may view or download stale artifacts, but cannot “resume anyway.”
They can start a new run whose prompt includes a bounded summary or selected
artifacts from the old run.

## Native Claude runner

Patch Watcher should own a small `ClaudeRunner` interface and a native
implementation over Claude Code's structured stream protocol. Claude Voice
Control is a useful reference for persistent conversations, structured event
capture, interruption, resumption, and separate turn/task state, but it is not
a runtime dependency and its registry/session model is not copied wholesale.

### Session ownership

- Create one managed Claude session per Patch Watcher run, named like
  `pw-68160-ps4-a1b2c3`.
- A session may have many turns and human messages during that run.
- A new Gerrit revision gets a new run and session. This prevents stale context
  from silently governing a new patchset.
- Store `run_id`, patch/change, patchset, SHA, policy version, capability
  profile, checkout path, and session-resource identifiers in Patch Watcher.
- Archive, rather than delete, terminal sessions after their retention policy
  permits it.

### API boundary

The `ClaudeRunner` interface provides:

- start/send/status/list/interrupt/stop/archive;
- JSON results and typed errors;
- an event cursor or bounded event tail;
- session/task/turn identifiers and timestamps.

The first implementation may borrow and simplify the cctty/streaming code from
Claude Voice Control, but it lives behind the Patch Watcher-owned interface.
If both applications later need the same stable implementation, extract a
small independent runner library rather than making either application depend
on the other or vendoring two drifting copies.

The runner remains the source of process facts, while the Patch Watcher
database owns workflow facts. On startup the reconciler compares both and
adopts, resumes, or marks sessions orphaned without silently duplicating them.

### Continuation behavior

Patch Watcher decides workflow continuation. There is no markerless automatic
continuation for managed runs. A valid structured `in_progress` report may
cause the dispatcher to send the next bounded prompt after checking revision,
policy, budgets, and pending operator messages. Write-capable runs cross that
controller policy boundary between every turn.

### Worker report protocol

The familiar `WORKER_STATUS` marker may remain as a human-readable fallback,
but require one machine-readable report at the end of each agent turn. A
versioned envelope should contain:

```json
{
  "schema": "patch-watcher-worker/v1",
  "run_id": "...",
  "state": "in_progress|waiting_human|blocked|complete",
  "summary": "short human-readable summary",
  "current_step": "what is happening now",
  "question": {"id": "optional-stable-id", "text": "...", "choices": []},
  "artifacts": [{"kind": "report", "path": "...", "description": "..."}],
  "requested_actions": [{"type": "...", "parameters": {}}]
}
```

The controller validates schema, `run_id`, paths, capability, and revision.
Invalid or missing reports are visible protocol errors; they do not authorize
actions. Text and tool-stream events remain available as activity and logs.

## Human messaging and control

The patch detail page contains a run conversation and a message composer. A
message is first committed to `run_message`; delivery is asynchronous and has
visible states: `queued`, `sent`, `acknowledged`, `failed`, or `superseded`.

### Message behavior by run state

- **Running:** default delivery waits for the current turn boundary, avoiding
  accidental interruption during a command. The operator may explicitly choose
  **Interrupt and send**, which records the interrupt and then delivers after
  the runner confirms the turn stopped.
- **Waiting for human:** the response targets the displayed question ID. On
  successful delivery the run returns to `queued`/`running`.
- **Waiting external:** a message may be queued, but does not automatically
  cancel the wait unless the operator also chooses resume/change course.
- **Paused:** a message is stored; **Resume with this message** is a separate
  explicit choice.
- **Blocked:** a message may explain a repaired dependency, but the controller
  rechecks the dependency before resuming.
- **Terminal or stale:** the composer offers **Start follow-up run**. It never
  resumes the old run silently.

Each submission includes the expected run version and optional question ID so
an answer cannot accidentally target a superseded state. Duplicate browser
submissions use an idempotency token. The timeline shows author, delivery
state, target turn/question, and when Claude next produced activity.

Operator run controls:

- **Pause after current step**;
- **Interrupt turn**;
- **Stop and cancel**;
- **Resume**;
- **Retry as a new run**;
- **Supersede with a new run**;
- **Archive session** after completion;
- **Open bounded/raw log** and artifacts.

Interrupting one Claude turn does not destroy its checkout or VMs. Once the
session/run becomes terminal, Patch Watcher automatically collects configured
artifacts and schedules all resources created by that session for purge. An
operator may explicitly retain a resource for debugging; retained resources
remain prominent until their retention ends or the operator purges them.

## Waiting for human contract

`waiting_human` is a resumable paused state, not success or failure. Entering
it requires:

- one concise question with a stable ID;
- why the decision is required;
- what the worker already tried;
- exact pinned revision and current-step context;
- relevant bounded logs/artifacts;
- suggested choices and a recommended safe default when appropriate;
- what will happen after each answer;
- a notification record.

No automatic turn continues while this state is active. Repeated triggers are
coalesced. On reply, the controller first verifies the revision and policy. A
new patchset makes the old run stale and offers to start a new run with the
answer attached.

`blocked` is reserved for a broken dependency or environment (authentication,
missing target, unavailable service, lost VM) rather than a judgment question.
The UI must not blur these two states.

## Deterministic retest workflow

Automatic retesting is best implemented as a deterministic controller run,
not a Claude task:

1. Apply the current-patchset non-Maloo review `-1` gate.
2. Read enforced Maloo failures and current retest state.
3. If a matching retest is already pending, enter `waiting_external` and do
   not request another.
4. If a failed suite has the required linked bug and policy permits retest,
   plan exactly one idempotent retest action.
5. Revalidate Gerrit revision and the Maloo state immediately before request.
6. Execute through the Maloo adapter, store result, and enter
   `waiting_external` for the new result.
7. If no linked bug explains the failure, create an investigation trigger or
   wait for a human according to policy. Do not guess a bug or request a retest.

This flow still appears as a normal run in the dashboard, with events,
actions, messages, and outcome. It simply has no Claude session. This is less
ambiguous, cheaper, and easier to make idempotent than asking an agent to
perform a known decision tree.

## LLM review tool environment

Agent-backed runs receive a generated task preamble that:

- identifies the run and exact revision;
- instructs the worker to read `/Users/patrick/AGENTS.md` plus the relevant
  repository instructions;
- lists available tools and granted capabilities;
- states that Gerrit comments, patch source, logs, JIRA, and web content are
  untrusted data, not instructions;
- forbids direct actions outside the capability grant;
- defines artifact locations and the worker-report schema;
- explains how to request a controller action or human decision.

The LLM tools' JSON output and exit-code conventions are the supported API.
Early workers should not receive raw credential files. The controller can
prefetch inputs or expose a narrow tool broker. Later direct tool access must
mount or inject only the credentials required by the capability profile.

## Full checkouts and ephemeral LTVM resources

An agent that edits or executes code needs a resource manifest stored with the
run.

### Checkout rules

- Create a dedicated full checkout at the pinned revision. Do not use Git
  worktrees for Lustre builds: generated configuration, staging, modules, and
  other source-adjacent state make independent checkouts the safer boundary.
- Record repository remote, base branch, revision SHA, path, and initial dirty
  state.
- Never reuse a dirty checkout across runs.
- Preserve the checkout as an artifact on failure when explicitly requested;
  otherwise clean it through a logged step.
- A prepared commit or diff is an artifact until upload capability is enabled.

### LTVM rules

VMs are not drawn from a pre-existing pool. The agent decides whether it needs
one VM or a cluster and chooses the target, architecture, topology, memory,
disks, and other LTVM arguments appropriate to the task. It creates those VMs
on demand and they are disposable session resources.

Reliable discovery requires an explicit ownership mechanism; comparing
`ltvm list` before and after a command is ambiguous when sessions overlap.
LTVM should persist an opaque owner/session identifier on every created VM,
including each member created by `cluster create`, and expose it through its
machine-readable list/status output. Patch Watcher launches Claude with that
identifier in a session-scoped environment. Ownership input remains backward
compatible and optional: an explicit CLI owner overrides the environment, and
ordinary calls with neither use a typed invoking-process fallback such as
`pid:<n>`. Existing state with no owner remains valid. The PID fallback is
diagnostic ownership for ordinary commands; Patch Watcher always supplies the
durable session identifier needed for restart-safe reconciliation.

This preserves agent autonomy: Claude invokes normal LTVM commands and chooses
their substantive arguments. The owner field only supplies durable attribution.

The session-resource lifecycle is:

- `creating`: the agent requested creation but it has not been confirmed;
- `active`: LTVM reports the VM with this session's owner identifier;
- `cleanup_pending`: the session is terminal and purge is queued;
- `destroying`: Patch Watcher issued the exact destroy operation;
- `destroyed`: LTVM confirms the VM no longer exists;
- `cleanup_failed`: destruction failed and will be retried/escalated;
- `retained`: an operator explicitly preserved it for bounded debugging;
- `orphaned`: ownership exists but Patch Watcher cannot match a healthy session.

Operational rules:

- The reconciler queries LTVM's machine-readable inventory and associates VMs
  by the durable owner/session identifier, not only by name or process ID.
- Use descriptive names containing the session/run identity and a
  filesystem-safe checkout identifier as an additional human aid.
- Check published targets before building, validate the target against the
  pinned Lustre checkout, and default to 2 GiB unless the task needs more.
- Record target, architecture, kernel, page size when known, variant, topology,
  vCPU, memory, disks, create command/result, and deployment revision.
- Capture commands, exit status, bounded output, console logs, and result
  artifacts.
- On `succeeded`, `failed`, `cancelled`, or `stale`, first collect configured
  artifacts and then purge every VM/cluster owned by the session. Cleanup is a
  durable, retried finalization step; the run remains visibly cleaning until
  LTVM confirms removal.
- `waiting_human`, `waiting_external`, `paused`, and recoverable `blocked`
  sessions retain their VMs because the session is not finished.
- Retention is an explicit operator exception with a visible expiry. After the
  retention period, the exact owner-recorded resources return to cleanup.
- A global resources page shows every Patch Watcher-owned VM, session, state,
  age, last-seen time, and cleanup error.
- Never destroy a VM whose durable owner cannot be verified as a Patch Watcher
  session. Surface unmatched resources for human reconciliation instead.

LTVM isolates the code being tested. It does not isolate the Claude process or
its host credentials, so it is not a replacement for worker containerization.

## Worker isolation and network roadmap

Initial read-only Claude runs may execute on the host for speed. The dashboard
must display **Unsandboxed host worker** and policy must prevent these workers
from building untrusted code or receiving broad write credentials.

Before general source-editing or autonomous operation, add worker isolation:

- rootless container, non-root user, read-only base image;
- only the run checkout and per-run scratch directory mounted writable;
- no host home directory, SSH agent, Docker/Podman socket, runner socket, or broad
  credential directory mounted;
- CPU, memory, process, disk, runtime, and output limits;
- controller-mediated access to LTVM and external write actions;
- per-capability credential injection with automatic expiry/cleanup;
- explicit network profile recorded in policy and run history.

Network profiles should eventually include:

- `host-unrestricted` (initial legacy mode, prominently labeled);
- `container-standard` (normal outbound access, still isolated from host);
- `container-restricted` (egress allowlist for the model endpoint and local
  controller/tool broker; no arbitrary direct internet);
- `container-offline-tools` (tool execution has no network; the host broker
  supplies prefetched inputs and mediates model transport/actions).

A Claude Code process normally needs access to its model service, so “no
internet” must be implemented either as allowlisted model-only egress or by
separating the model transport from a no-network tool sandbox. The UI should
not claim full network denial while silently allowing general egress.

Containerization is a later track, but it becomes an entry criterion before
enabling broad code execution, durable credentials, or an autonomous lane.

## Dashboard information architecture

### Global overview

Show:

- observer/scheduler health and last successful poll;
- global automation state and emergency stop;
- counts of queued, running, waiting-human, waiting-external, blocked, failed,
  and stale runs;
- available/busy Claude slots plus active, retained, orphaned, and
  cleanup-failed LTVM resources;
- recent errors and unsent notifications;
- default policy and worker isolation mode.

### Patch table

Keep the compact patch/review/CI presentation, and add:

- effective automation summary (for example `Retest: auto`, `Research: manual`);
- active-run badge and current step;
- last agent/controller activity;
- prominent waiting-human/blocked/stale indicator;
- one link to patch detail.

Merged or abandoned patches retain terminal watch-state behavior and do not
start new runs.

### Patch detail

Sections:

1. Current Gerrit/review/CI observation and exact revision.
2. Effective policy, inherited defaults, pending edits, and safety budgets.
3. Active run card: state, reason, step, model, runtime, last activity,
   full checkout, session-created VMs, capabilities, isolation/network profile.
4. Pending human question or blocker, displayed above routine logs.
5. Conversation and message composer with delivery state and interrupt option.
6. Timeline of observations, triggers, policy decisions, messages, worker
   reports, actions, errors, and recovery events.
7. Artifacts and bounded/raw logs.
8. Prior runs, including stale and cancelled runs.

### Controls and safety in the UI

- Separate **refresh observation** from **start run**.
- Explain why an action is enabled, disabled, suppressed, or waiting.
- Preview the effective policy and exact capability changes before saving.
- Show `disabled`, `advise`, `approval`, or `automatic` beside every workflow
  capability; never rely on a generic “enabled” label for write behavior.
- Provide a policy **dry-run** that evaluates the current patch without acting.
- Require confirmation for interrupt, cancel, destructive cleanup, credentials,
  uploads, votes, or autonomous enabling.
- Use text plus color/icons; never encode state by color alone.

## Recovery, timeouts, and reconciliation

On startup and periodically:

1. Mark non-terminal runs `recovering` internally while keeping their last
   user-facing state visible.
2. Compare each run with its Claude runner session, action attempts, full
   checkout, and owner-attributed LTVM inventory.
3. Adopt a healthy running Claude process rather than starting another.
4. If a host died, resume only when policy permits and no action is ambiguous;
   otherwise mark blocked with a specific recovery action.
5. Reconcile every `executing` action against remote state before retry.
6. Verify revision freshness.
7. Continue terminal cleanup, retain explicit debugging resources, and surface
   any owner-attributed resources that no longer match a healthy session.
8. Record the recovery decision as an event.

Use distinct limits for:

- no-agent-output warning;
- current command/step deadline;
- total run runtime;
- Claude turn and continuation count;
- adapter retries/backoff;
- external action count;
- retained log/artifact size.

No-output alone is not proof of a hung build or test. A worker can report a
long-running command identifier/deadline, and the reconciler checks the owned
process or VM before escalating. Never use self-matching process polls.

Retry only operations known to be safe and idempotent. Exhaustion becomes a
visible `blocked` or `failed` outcome, not an invisible loop.

## Notifications and reports

- Immediate notification for `waiting_human`, blocked infrastructure,
  repeated failure, emergency stop, and ambiguous external action.
- Daily email summarizes observations, runs, actions, errors, and unanswered
  questions.
- Test-email remains available and contains a bounded recent summary.
- Notification delivery is itself idempotent and logged.
- Email is a notification channel, not an authorization channel; replies do
  not control runs until an authenticated reply workflow is separately built.

## Security and trust boundaries

- Bind locally until deployed behind authenticated TLS (for example a reverse
  proxy on Mulberry Server). Add application sessions/CSRF protection before
  accepting remote commands.
- Keep private config mode `0600`; never show credentials in the UI, prompts,
  logs, subprocess arguments, or artifacts.
- Treat patch code and all remote text as attacker-controlled data.
- Do not expose generic shell execution through dashboard fields.
- Build/test untrusted code only in an owned LTVM guest or approved sandbox.
- Redact tokens, cookies, passwords, SSH material, and sensitive environment
  values before storing tool output.
- Record actor identity for operator commands once multi-user access exists.
- Use least-privilege service and container identities.
- Back up the SQLite database and keep event/artifact retention configurable.

## Phased implementation plan

Each phase is deployable and has a hard exit test. Later controls may be shown
disabled as design previews, but must not imply functionality.

### Phase 0A: durable observer

Build:

- SQLite schema/migrations for patches, revisions, observations, policies,
  triggers, events, notifications, and service cursors;
- migrate seed-file/in-memory patches without losing the current UI;
- independent scheduler/observer and manual Refresh All command;
- persistent normalized history and error log;
- global/patch automation flags, both off;
- service health and last-poll display.

Exit criteria:

- watch list/history survive restart;
- no browser is required for scheduled polling;
- concurrent refresh requests do not duplicate observations/triggers;
- a changed patchset is represented as a new exact revision;
- current tests plus migration/restart tests pass.

### Phase 0B: run control and manual read-only agent

Build:

- run/event/message/action/resource schema;
- dispatcher and startup reconciler;
- native `ClaudeRunner` over the structured stream protocol;
- one manual **Investigate** run pinned to a revision with read-only tools;
- run detail page, conversation, waiting-human question, message delivery,
  pause/interrupt/cancel/resume/follow-up controls;
- structured worker report validation;
- clearly visible unsandboxed-worker label.

Exit criteria:

- one patch cannot obtain two active runs under race;
- human messages deliver exactly once and their state is visible;
- waiting-human survives service restart and resumes only after a valid answer;
- terminal/stale runs cannot be silently resumed;
- a live Claude runner session is adopted after Patch Watcher restart;
- no external write capability is present.

### Phase 1: deterministic automatic retest

Build:

- full top-level review gate and Maloo test flow;
- fingerprinted triggers and deterministic controller runner;
- linked-bug and pending-retest checks;
- action outbox, idempotency, ambiguous-call reconciliation;
- per-patch automatic/manual test policy, budgets, dry-run preview;
- `waiting_external` result polling and dashboard timeline;
- immediate/daily notifications.

Exit criteria:

- the same failure cannot produce two retest requests under repeated polls,
  restarts, or concurrent refreshes;
- a non-Maloo `-1`, pending retest, unknown failure, disabled policy, and stale
  patchset each suppress action with the correct visible reason;
- simulated crash during request reconciles remote state before retry;
- revision changes between planning and execution prevent the request;
- no Claude session is required for the mechanical path.

### Phase 2: unknown-failure research agent

Build:

- automatic or manual investigation trigger for failures without linked bugs;
- read-only Gerrit/Maloo/JIRA/repository evidence bundle;
- evidence report, recommendation, and human escalation;
- runtime/turn budgets and bounded artifact/log display;
- prompt-injection defenses and controller-owned tool access.

Exit criteria:

- agent cannot retest, comment, vote, edit, upload, or access ungranted secrets;
- every factual recommendation links to captured evidence;
- malformed/missing reports block cleanly;
- human can message, redirect, or stop the run from the dashboard;
- a newer patchset stales the run before its recommendation can be acted on.

### Phase 3: isolated execution foundation

Build:

- full independent checkout lifecycle;
- session-scoped LTVM ownership metadata and reconciliation;
- agent-driven, on-demand VM/cluster creation with target
  list/fetch/validate guidance and recorded VM environment;
- safe command/test manifests rather than arbitrary dashboard shell text;
- artifact collection, cleanup, quarantine, and orphan reconciliation;
- rootless worker container prototype and network-profile display.

Exit criteria:

- untrusted build/test code does not execute in the web service or host worker
  context;
- two runs cannot share a writable checkout or owner-attributed VM;
- cancellation and restart do not destroy unrelated VMs;
- environment and test results are reproducible from the run manifest;
- restricted-egress behavior is tested and honestly represented.

### Phase 4: review handling and proposed edits

Build:

- implement **Handle simple comments** and **Handle all comments** policies;
- exact current-patchset review-comment snapshot;
- isolated edit/build/test run;
- proposed diff/commit and reply draft as artifacts only;
- explicit human approval and fail-to-human behavior.

Exit criteria:

- “simple” mode escalates any ambiguous/nontrivial comment without attempting
  it;
- “all” mode attempts broadly but still escalates uncertainty;
- neither mode posts or uploads automatically;
- all edits map to a specific comment and pinned revision;
- containerization/isolation gate is met before executing patch code.

### Phase 5: Jenkins build handling and controlled Gerrit writes

Build:

- design and implement Jenkins failure classification separately from Maloo
  retesting;
- narrowly granted Gerrit replies/messages and patchset upload;
- pre-write revision revalidation, approval, idempotency, and audit;
- post-upload observation of the new patchset as a new revision/run boundary.

Exit criteria:

- every write is previewed or policy-authorized, revision-pinned, and
  controller-mediated;
- an upload never targets or rebases over an unexpected patchset;
- ambiguous posts/uploads reconcile before retry;
- rollback/recovery and human escalation are tested.

### Phase 6: autonomous lanes

Build:

- named lane definitions for proven-safe patch classes;
- eligibility checks, limited capabilities, budgets, test requirements, and
  automatic stop conditions;
- global/per-project/per-patch kill switches;
- outcome metrics and periodic policy review.

Exit criteria:

- a lane cannot expand its own eligibility or capabilities;
- dry-run/replay demonstrates expected decisions over historical data;
- every autonomous result has a complete audit trail;
- failures, uncertainty, policy drift, and unexpected external state fail to a
  human rather than improvising.

## Test strategy

Build a fake-adapter test harness before enabling actions. Required suites:

- table-driven evaluator and state-transition tests;
- SQLite migration, constraint, transaction, and crash-restart tests;
- duplicate poll/trigger/run/action races;
- stale patchset at every transition and immediately before writes;
- Claude runner lost process, idle turn, invalid marker/report, needs-input, interrupt, and
  resume behavior;
- human message idempotency, stale question, queued delivery, and terminal-run
  follow-up behavior;
- external action success/failure/timeout/ambiguous/reconciliation;
- LTVM owner propagation, terminal purge, orphan, cleanup retry, retention, and
  unrelated-VM protection;
- capability-denial, secret-redaction, prompt-injection, CSRF, and auth tests;
- event replay: rebuild current projections from a recorded event sequence;
- end-to-end dry-run with fake Gerrit/Maloo/Jenkins/JIRA/Claude/LTVM adapters.

Production integrations get explicit opt-in integration tests. The default
test suite performs no network requests, sends no mail, changes no Gerrit
state, and creates no VM.

## Implementation decisions to settle before each phase

These should not block Phase 0, but each must be resolved before its dependent
capability is enabled:

- service manager on Mac versus Linux deployment target;
- authenticated remote access design for Mulberry Server;
- exact native Claude runner interface and structured-event contract;
- Maloo's remote identifiers and best reconciliation query for ambiguous
  retest calls;
- artifact retention limits and backup location;
- agent model/effort and budget accounting source;
- container runtime and model-endpoint network strategy;
- credential broker design;
- retained-VM expiry and artifact collection policy before automatic purge;
- Gerrit identity and approval policy for eventual automated writes.

No unresolved decision should be hidden behind an enabled checkbox. The
dashboard should show the capability as unavailable and explain the missing
prerequisite.
