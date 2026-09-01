# Patch Watcher Agent Orchestration Design

This document turns the Patch Watcher action flow into an implementation
contract. `DESIGN_ACTION_FLOW.md` remains the product-policy description: it
defines when test, build, and review conditions matter. This document defines
how Patch Watcher observes those conditions, starts controlled work, exposes
that work to a person, and recovers safely.
`WORKER_ENVIRONMENT_CONTRACT.md` defines the separate, versioned environment
contract that must be admitted before a Claude process starts.

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

## Product surfaces and future work entry modes

The current implementation is **patch-centric**, but the managed engineer
infrastructure should not be permanently coupled to Gerrit observations. The
long-term product has three distinct ways to create work:

1. **Watched Gerrit patch.** Patch Watcher observes a change, applies its
   configured policy, and may start a revision-pinned run to investigate or
   advance that patch. This remains the primary workflow covered by the phased
   plan below.
2. **Jira-ticket engineering.** A person supplies a Jira issue key. The system
   retrieves and snapshots the ticket, establishes the relevant project,
   repositories, source baselines, acceptance criteria, and permitted actions,
   and starts an engineering run whose goal is the ticket rather than an
   already-existing Gerrit change. That run may eventually create one or more
   patches, so its identity and lifecycle cannot be modeled as merely another
   watched-patch run.
3. **Free-form engineering.** A person supplies an arbitrary prompt plus
   explicit project/repository and environment context. This is a separate,
   more general entry point and must not be smuggled into the patch
   investigation text box. It needs its own validation, provenance,
   permissions, budgets, and approval behavior.

The dashboard should eventually put the second and third modes on a separate
**Engineering work** page or clearly separate section, not in each watched
patch row. That surface can offer **Start from Jira ticket** and **Start from
prompt** while reusing the same durable run, session, message, resource,
timeout, human-intervention, and audit machinery.

This is a design direction only. Neither new entry point is enabled in Phase
0C, and the current **Investigate** action must remain narrowly pinned to an
existing Gerrit revision. Jira content and a user-authored prompt are task
inputs, not authority: they cannot grant tools, credentials, external writes,
or broader network access. The controller must still create the capability
envelope independently.

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
13. The worker environment is still implicitly Patrick's Mac account: tools,
    versions, paths, instructions, credentials, and host services are not an
    attestable portable contract.

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
5. **Remote engineering actions are controller-owned.** Gerrit, Maloo,
   Jenkins, JIRA, and patch-upload writes are requested by workers and executed
   only after the controller validates policy, revision, budget, and
   idempotency. A worker with the LTVM capability may directly create the
   ephemeral VM topology it needs; session ownership metadata makes those
   local resources discoverable and terminal cleanup remains Patch Watcher's
   responsibility.
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
11. **Admission precedes execution.** A worker profile, run envelope, and
    successful environment attestation are persisted before Claude starts.
    Missing requirements block visibly; they never trigger a silent fallback
    to an ambient or more privileged environment.

## Vocabulary and separate state domains

The UI and code should use these terms consistently:

- **Patch:** a watched Gerrit change, independent of patchset.
- **Work item:** the durable top-level objective for a run. It is a Gerrit
  change today; future work items may instead be a snapshotted Jira ticket or
  a user-authored free-form engineering request.
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

Future ticket and free-form modes also need a work-item state distinct from
any patches they later create. A ticket run that produces two Gerrit changes
must remain one ticket work item with two separately revision-pinned patch
outputs; it must not silently turn into either patch's watcher state.

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
- **Direct LTVM capability:** launches Claude with `LTVM_OWNER_ID` and allows
  it to invoke normal LTVM commands, choosing the target, topology, and VM
  parameters itself. Patch Watcher discovers the resulting VMs through LTVM's
  persisted `owner_id` and handles terminal cleanup.

### Tool adapters

Wrap `gc`, `maloo`, `jenkins`, `jira`, and later `janitor` using their JSON
output and documented exit codes. Each adapter returns a normalized typed
result while retaining a redacted raw-result reference for debugging. Do not
parse human-oriented terminal tables.

### Resource sampler

- Samples the worker host independently of the browser and records sample
  freshness and collection errors.
- Reads total, available, and used host memory plus swap and memory-pressure
  indicators from a supported OS API.
- Measures each live Claude process tree rather than only the parent process.
  Prefer proportional set size (PSS) where the OS exposes it; otherwise label
  resident set size (RSS) as an estimate that may double-count shared pages.
- Inventories all current LTVM VMs. For each VM, keep configured guest memory
  separate from the actual host memory used by its VM process; neither number
  is silently substituted for the other.
- Associates a VM with a Patch Watcher session only when its durable LTVM
  `owner_id` matches that session. Legacy or externally owned VMs remain
  visible as unassociated resources and are never adopted for cleanup.
- Samples more frequently while sessions are active and downsamples or expires
  old detail according to a retention policy. The current dashboard never
  presents a stale sample without its timestamp and warning.

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
| `run` | patch/revision, trigger, policy snapshot, type, execution profile, effective timeout limits, state, summary/question/error, started/last-qualifying-activity/deadline timestamps, version |
| `agent_session` | run, runner/session id, worker host, process identity, state, started/last-event/ended timestamps |
| `run_event` | run, monotonic sequence, actor, type, structured payload, timestamp |
| `run_message` | run, author, body, urgency, delivery state, target question/turn, timestamps |
| `action_attempt` | run, action type, idempotency key, state, request/result, timestamps |
| `session_resource` | run/session, type, name, create request, environment, lifecycle state, last seen, cleanup result |
| `artifact` | run, kind, path/URI, content hash, size, description, retention state |
| `notification` | run/patch, type, destination, idempotency key, delivery state/result |
| `worker_host` | stable identity, display name, OS/architecture, total memory, last seen, sampler state/error |
| `resource_sample` | host/session/resource scope, measured time, CPU, RSS/PSS, configured guest memory, swap/pressure fields, quality/source |
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
- `resource_exhausted`: terminal outcome for a run that could not obtain the
  local LTVM CPU, memory, disk, address, or other host capacity it requested.
  It schedules session-resource cleanup and a per-patch retry cooldown.
- `succeeded`: intended bounded outcome completed.
- `failed`: attempt ended unsuccessfully and is not automatically continuing.
- `cancelled`: operator stopped the run.
- `stale`: pinned Gerrit revision is no longer current.

All transitions use optimistic concurrency (`run.version`) and are recorded in
`run_event`. `waiting_human`, `waiting_external`, `paused`, and `blocked` keep
the logical one-active-run-per-patch claim. If a new patchset appears during an
active Claude session, the reconciler sets a separate `superseded_revision`
flag but does not interrupt or inject a message into the session. The active
run keeps the patch claim until the session ends; it then becomes `stale`,
releases mutable resources according to policy, and permits evaluation of the
new revision.

### Claude state mapping

The native runner should retain Claude Voice Control's useful distinction
between turn state and task state rather than collapsing them:

| Claude runner observation | Patch Watcher interpretation |
| --- | --- |
| turn `running` | session is active; run usually remains `running` |
| turn `waiting`/`idle`, task `in_progress` | turn ended; controller evaluates the structured report before continuing |
| task `needs_input` | validate question and move run to `waiting_human` |
| task `blocked` | move to `blocked`, unless a clear human question makes `waiting_human` more accurate |
| structured `resource_exhausted` report | validate the LTVM evidence, mark the run `resource_exhausted`, notify, clean up, and start cooldown |
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
maloo-retest:<change>:<revision-sha>:<session-id>
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
- execution profile (`triage` or `engineering`) and timeout overrides;
- capability grant;
- runtime, turn, retry, external-action, and optional cost budgets;
- initial/maximum LTVM resource-exhaustion cooldown and retry policy;
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
3. records the active run as superseded without sending an unsolicited message
   into its Claude session;
4. lets the worker finish producing logs or artifacts against its original
   revision, but allows no external write;
5. marks the run `stale` when its session ends;
6. preserves its logs and artifacts;
7. cleans its checkout and session-created VMs, unless an operator has
   explicitly retained a resource for debugging; and
8. lets the new revision create its own trigger/run after the old run releases
   the per-patch active-run claim.

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

### New patch activity while a session is running

New Gerrit, review, or CI observations are recorded and shown while a Claude
session is running, but they are not injected into that session. Patch Watcher
does not interrupt, restart, redirect, or otherwise interact with the session
in response to the new observation. The worker continues against the revision
and evidence snapshot with which it started.

If the observation contains a newer patchset, the active run receives a
visible **Superseded revision** flag while the worker finishes its current
bounded work. This is a flag, not a new run state. The worker may still produce
logs or artifacts, but the mandatory pre-write revision guard rejects all
external side effects from the old revision and its result is ultimately
recorded as stale. After the run reaches a terminal state, the evaluator
considers the newest observation and may create a new run. This deliberately
avoids trying to merge new context into a live engineering session.

### Worker report protocol

The familiar `WORKER_STATUS` marker may remain as a human-readable fallback,
but require one machine-readable report at the end of each agent turn. A
versioned envelope should contain:

```json
{
  "schema": "patch-watcher-worker/v1",
  "run_id": "...",
  "state": "in_progress|waiting_human|blocked|resource_exhausted|complete",
  "summary": "short human-readable summary",
  "current_step": "what is happening now",
  "question": {"id": "optional-stable-id", "text": "...", "choices": []},
  "artifacts": [{"kind": "report", "path": "...", "description": "..."}],
  "error": {
    "code": "optional-machine-readable-code",
    "operation": "optional failed operation",
    "requested": {},
    "evidence": "bounded sanitized evidence",
    "retryable": true
  },
  "requested_actions": [{"type": "...", "parameters": {}}]
}
```

The controller validates schema, `run_id`, paths, capability, and revision.
Invalid or missing reports are visible protocol errors; they do not authorize
actions. Text and tool-stream events remain available as activity and logs.

## Agent execution profiles and timeouts

Agent-backed work uses one of two explicit execution profiles. The profile and
its effective limits are snapshotted into the run so later configuration
changes cannot silently change an active session.

### Triage profile

Use `triage` for short sessions that inspect Gerrit, review, CI, JIRA, or source
information and may request a small number of controller-mediated actions. A
triage run must not create LTVM VMs. Its default maximum wall-clock runtime is
20 minutes, measured from successful Claude process start. Reaching that
deadline fails the run with `agent_runtime_timeout`; activity does not extend
the deadline. An operator may explicitly extend the deadline before it expires,
and that change is recorded as an event.

### Engineering profile

Use `engineering` for debugging, patch development, builds, tests, and other
work that may create LTVM VMs. These runs do not have a short wall-clock limit.
Instead, the default failure threshold is 30 minutes without qualifying
activity while the run is `preparing` or `running`. Reaching the threshold
fails the run with `agent_inactivity_timeout`.

Qualifying activity is evidence that the owned work is advancing, including:

- new Claude output or a valid structured worker-status event;
- an owned tool or command starting or completing;
- new output from an owned command;
- changing CPU, I/O, or explicit progress counters for an owned long-running
  command; or
- a state transition from an owner-attributed session VM or VM cluster.

A dashboard refresh, observer poll, repeated unchanged process/VM status, or
activity from an unrelated process does not reset the clock. Long-running
wrappers must expose enough bounded progress or owned-process telemetry to
avoid treating a legitimately active build or test as silent.

The inactivity clock runs only in `preparing` and `running`. It is suspended in
`waiting_human`, `waiting_external`, `paused`, and `blocked`, because those
states already identify why progress is intentionally stopped. Resumption
starts a fresh inactivity interval and records that transition.

### Long-running notices and absolute cap

Every agent session also has a nonextendable absolute wall-clock cap of 48
hours. Triage sessions ordinarily hit their 20-minute limit first. A
non-terminal engineering session may continue as long as it is making
progress, but Patch Watcher sends a status email when its wall-clock age
reaches two hours and every two hours thereafter, including while it is in a
waiting state. Each reminder has an interval-derived idempotency key, and a
service restart must not resend an interval already recorded as delivered.

The reminder is informational: it does not pause the worker or reset any
clock. It includes the patch and run, elapsed time, current step, last
qualifying activity, bounded excerpts of the most recent session messages,
current resource use, owned VMs, and an authenticated **Kill session** link.
The link opens the run page with the destructive action ready for explicit
confirmation; an email GET request must never kill a process by itself.

At 48 hours Patch Watcher fails the run with `agent_absolute_runtime_cap` and
uses the same stop, artifact collection, notification, and owner-scoped cleanup
path as other timeouts. The cap cannot be extended from the dashboard.

### Timeout response

When either timeout fires, Patch Watcher must:

1. transactionally re-read the run version, state, deadline, and last
   qualifying activity so activity racing with the timeout wins; if the
   deadline is still expired, atomically mark the run `failed` with the exact
   timeout code, configured limit, start time, and last activity time;
2. interrupt Claude and, after a bounded grace period, stop the process if it
   has not exited;
3. collect available logs and artifacts before terminal cleanup;
4. purge only the checkout and LTVM resources owned by that run/session;
5. send one immediate idempotent email with the patch, run, timeout reason,
   last activity, cleanup state, and dashboard link; and
6. show **Failed — 20-minute runtime limit**, **Failed — inactive for 30
   minutes**, or **Failed — 48-hour absolute limit** on the patch and run
   pages.

The dashboard shows the profile, start time, last qualifying activity, current
step, and remaining runtime or inactivity time for every active agent run. A
timeout never silently restarts or resumes a session; retry creates a new run.

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

The active-run card exposes **Send guidance** as the operator's deliberate
“prod.” It can clarify or change the requested approach after the operator has
reviewed recent activity. By default it queues guidance for the next safe turn
boundary; **Interrupt and send** is a separate, more disruptive choice. This
human control is distinct from patch observation: Patch Watcher never injects
new Gerrit or CI activity into a running session automatically.

Operator run controls:

- **Pause after current step**;
- **Interrupt turn**;
- **Stop and cancel**;
- **Kill session**, which requires confirmation, stops the Claude process,
  records who requested it, marks the run cancelled, and begins owner-scoped
  artifact collection and cleanup;
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
- incorporates versioned portable organization policy derived from the useful
  parts of the user-level `AGENTS.md`, plus the relevant repository
  instructions, and snapshots the resulting instruction text and hash;
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

The complete worker-box boundary, logical paths, tool/profile model,
admission `doctor`, environment attestation, host services, and portability
phases are defined in `WORKER_ENVIRONMENT_CONTRACT.md`. A user home directory,
shell startup file, editable install, or absolute workstation path is never a
portable worker dependency merely because it exists on the initial host.

### Worker rule: LTVM resource exhaustion

The generated instructions for every worker with `start_ltvm` capability must
include this rule:

1. If `ltvm create` or `ltvm cluster create` fails, inspect the bounded error
   once and distinguish resource exhaustion from invalid arguments, missing
   artifacts, authentication, or another infrastructure error.
2. Resource exhaustion means evidence such as insufficient host memory, disk,
   CPU/allocation capacity, address/slot capacity, or an explicit resource
   limit. Do not label an unknown creation failure as exhaustion.
3. Do not repeatedly retry with arbitrary variants, destroy other sessions'
   VMs, or attempt to free host resources.
4. Stop VM-dependent work and emit a structured worker report with:
   - state `resource_exhausted`;
   - error code `ltvm_resource_exhausted`;
   - failed LTVM operation;
   - requested topology/resources;
   - bounded, sanitized command output establishing the shortage; and
   - identifiers of any resources already created under this session owner.
5. For a non-resource creation failure, report `blocked` with a more accurate
   error code such as `ltvm_create_failed` instead.

On a validated `ltvm_resource_exhausted` report, Patch Watcher:

- records a first-class `resource_exhausted` run outcome and timeline event;
- stops that agent attempt and collects its diagnostics;
- purges any complete or partial VM/cluster resources carrying the session's
  `owner_id`, so the failed attempt does not worsen capacity pressure;
- sends an immediate status email containing the patch, run, requested
  resources, evidence, cleanup state, and dashboard link;
- sets a configurable `vm_retry_not_before` cooldown for that patch and
  suppresses automatic VM-backed runs until it expires;
- continues ordinary read-only patch observation during the cooldown; and
- exposes **Retry now**, **extend cooldown**, and **disable VM automation** to
  the operator. Manual retry requires confirmation and starts a new run rather
  than reviving the failed one.

Repeated exhaustion increases the cooldown up to a configured maximum and
raises a global LTVM-capacity warning on the dashboard. It does not silently
loop. The global warning is informational initially; it does not stop
unrelated read-only or non-VM workflows.

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

Reliable discovery uses LTVM's implemented ownership contract; comparing
`ltvm list` before and after a command is ambiguous when sessions overlap.
Patch Watcher launches Claude with a durable opaque value such as
`patch-watcher:<session-id>` in `LTVM_OWNER_ID`. Claude then invokes ordinary
commands without Patch Watcher choosing their substantive arguments:

```bash
ltvm create NAME ...
sudo ltvm cluster create NAME NODES...
```

LTVM resolves ownership once per create operation and persists it on the VM
and, for clusters, every member. The input contract is backward compatible:

1. explicit `--owner ID` or `--owner-id ID`;
2. `LTVM_OWNER_ID` from the invoking environment; or
3. automatic `pid:<invoking-ltvm-pid>` fallback.

Explicit ownership is optional, existing commands remain valid, and legacy VM
state with no owner remains valid. `ltvm list --json` returns `owner_id` for
each VM and returns `null` for legacy unowned VMs. Patch Watcher uses only its
durable session values for automatic reconciliation; a PID fallback is useful
diagnostic ownership but is not restart-stable enough to identify a Patch
Watcher session.

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
- On `succeeded`, `failed`, `resource_exhausted`, `cancelled`, or `stale`, first
  collect configured artifacts and then purge every VM/cluster owned by the
  session. Cleanup is a durable, retried finalization step; the run remains
  visibly cleaning until LTVM confirms removal.
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
- access to host LTVM through a narrow container bridge that preserves the
  agent-selected arguments and injects the session owner; controller-mediated
  external review/CI write actions;
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

The top of the page begins with a worker-host memory summary. For the initial
single-host deployment it shows host name, sample time, physical total,
available, used, cache/reclaimable memory when the OS reports it, swap use,
memory pressure, and the amount attributable to active Claude process trees and
LTVM VM processes. Configured guest RAM is shown alongside, but not added to
physical usage. If values cannot be measured or reconciled, display **unknown**
or **estimated** instead of manufacturing a total. The summary becomes warning
or critical at configurable available-memory/pressure thresholds.

The remainder of the overview shows:

- observer/scheduler health and last successful poll;
- global automation state and emergency stop;
- counts of queued, running, waiting-human, waiting-external, blocked, failed,
  resource-exhausted, and stale runs;
- available/busy Claude slots plus active, retained, orphaned, and
  cleanup-failed LTVM resources;
- recent errors and unsent notifications;
- default policy and worker isolation mode.

### Sessions and LTVM resources

The overview includes a collapsible active-sessions section and a link to a
full resources page:

- One row per current Claude session: patch, run, profile, state, elapsed time,
  last qualifying activity, most recent message summary, Claude process-tree
  memory, and current step.
- Expanding a session shows a bounded tail of recent messages/events, the full
  timeout countdowns, **Send guidance**, **Interrupt and send**, and **Kill
  session** controls.
- Owner-associated VMs are nested under their session. Each shows name,
  topology/role, state, age, configured guest memory, measured host memory when
  available, CPU use, and cleanup state.
- A separate **Other LTVM VMs** group shows every currently inventoried VM
  that has no matching Patch Watcher session, including legacy unowned VMs.
  These are observable but cannot be destroyed by Patch Watcher's automatic
  cleanup.
- Group and page totals avoid double counting. Claude process-tree memory and
  VM-process memory are separate components; host used/available memory remains
  the authoritative capacity view.

Every metric includes its sample age. A collection failure leaves the last
sample visible but clearly stale, logs the error, and prevents the inactivity
detector from treating missing telemetry as positive activity.

### Patch table

Keep the compact patch/review/CI presentation, and add:

- effective automation summary (for example `Retest: auto`, `Research: manual`);
- active-run badge and current step;
- last agent/controller activity and a one-line most-recent-message summary;
- prominent waiting-human/blocked/resource-exhausted/stale indicator;
- one link to patch detail.

Merged or abandoned patches retain terminal watch-state behavior and do not
start new runs.

### Patch detail

Sections:

1. Current Gerrit/review/CI observation and exact revision.
2. Effective policy, inherited defaults, pending edits, and safety budgets.
3. Active run card: state, reason, step, model, execution profile, runtime,
   last qualifying activity, timeout countdown, full checkout,
   session-created VMs, capabilities, and isolation/network profile.
4. Pending human question or blocker, displayed above routine logs.
5. Conversation and message composer with a bounded recent tail, expandable
   history, delivery state, **Send guidance**, and interrupt option.
6. Timeline of observations, triggers, policy decisions, messages, worker
   reports, actions, errors, and recovery events.
7. Artifacts and bounded/raw logs.
8. Prior runs, including resource-exhausted, stale, and cancelled runs.

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

- triage wall-clock runtime (20 minutes by default);
- engineering inactivity (30 minutes by default);
- current command/step deadline;
- absolute agent-session runtime (48 hours, nonextendable);
- Claude turn and continuation count;
- adapter retries/backoff;
- external action count;
- retained log/artifact size.

No-output alone is not proof of a hung build or test. For the engineering
profile, the reconciler also checks changing telemetry from the owned process
or owner-attributed VM before declaring inactivity. A worker can report a
long-running command identifier/deadline. Never use self-matching process
polls.

Retry only operations known to be safe and idempotent. Exhaustion becomes a
visible `blocked` or `failed` outcome, not an invisible loop.

## Notifications and reports

- Immediate notification for `waiting_human`, blocked infrastructure,
  LTVM resource exhaustion, agent runtime/inactivity timeout, repeated failure,
  emergency stop, and ambiguous external action.
- Active engineering sessions send a status reminder after two hours and every
  two hours thereafter. Reminders include bounded recent messages and an
  authenticated link to the run's confirmed Kill-session control.
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

### Current implementation checkpoint

Commit `05cc0df` delivered the first development slice: live host/LTVM resource
collection, the resource/session dashboard, private durable managed-session
state, timeout/reminder calculations, recent messages, and safe recorded
guidance/kill intents. It did **not** finish every item in formal Phase 0A, and
it did not add a Claude runner or perform process control.

The next executable sequence is:

1. finish the durable observer/scheduler and database projections in Phase 0A;
2. admit the worker environment through the new Phase 0B contract; and
3. exercise both with one manual, revision-pinned, read-only Claude run in
   Phase 0C, including live messages, operator guidance, stop/kill, restart
   adoption, and no external write authority.

This sequence keeps automatic retesting and all Gerrit/CI writes disabled while
proving the complete human-visible engineer lifecycle.

Phases 0B and 0C are now implemented in the repository. Phase 0B supplies the
strict worker/run/attestation contracts, truthful current-host profile,
private run layouts, portable instructions, admission doctor, durable
provenance, and dashboard evidence. Phase 0C supplies the narrow vertical
path: an operator-started, exact-revision, read-only investigation with
admission before launch, a reconnectable native runner, durable events and
messages, waiting-human and live guidance, confirmed stop/kill controls,
restart adoption, structured completion, timeouts and reminders, and
owner-scoped cleanup. It deliberately grants no Gerrit, CI, Jira, LTVM,
source-editing, shell, or upload capability.

Phase 1's deterministic automatic-retest path is now implemented. It reuses a
separate durable trigger/outbox/event ledger and keeps the mechanical decision
and idempotent remote action in the controller; it does not require a Claude
session for an already-understood retest decision. Phase 2's read-only research
agent is also implemented for failures that cannot pass Phase 1's evidence
rules. Its report cannot write externally. A narrow follow-on workflow can
associate an operator-supplied existing Jira key and then request a retest,
but those are separately approved controller actions with no agent authority.
The next implementation slice is Phase 3's isolated execution foundation.

### Phase 0A: durable observer

Build:

- SQLite schema/migrations for patches, revisions, observations, policies,
  triggers, events, notifications, and service cursors;
- migrate seed-file/in-memory patches without losing the current UI;
- independent scheduler/observer and manual Refresh All command;
- persistent normalized history and error log;
- worker-host resource sampler and top-level memory summary with freshness and
  collection-error states;
- global/patch automation flags, both off;
- service health and last-poll display.

Exit criteria:

- watch list/history survive restart;
- no browser is required for scheduled polling;
- concurrent refresh requests do not duplicate observations/triggers;
- a changed patchset is represented as a new exact revision;
- current tests plus migration/restart tests pass;
- host memory totals are sourced, timestamped, and do not confuse configured
  guest memory with physical host use.

### Phase 0B: worker environment admission

Build:

- schemas for the worker profile, per-run envelope, and environment
  attestation defined in `WORKER_ENVIRONMENT_CONTRACT.md`;
- the truthful `host-unsandboxed-mac-v1` compatibility profile;
- private logical run directories and generated, hashed portable worker
  instructions rather than a dependency on the operator's home directory;
- `pw-worker doctor` with offline tool/version, checkout, path, resource,
  broker/report-channel, and optional LTVM health checks;
- run persistence for profile/hash, environment instance, attestation,
  instruction hash, and broker session ID; and
- dashboard admission state, failed-preflight reason, provenance, and visible
  isolation/network profile.

Exit criteria:

- Claude is never started before a successful persisted attestation;
- missing tools, incompatible runtime versions, dirty/wrong checkouts,
  insufficient resources, and unavailable capabilities each block with a
  precise redacted reason;
- a worker may rely only on declared logical paths and capabilities, not
  Patrick-specific paths or dotfiles;
- the current host is labeled **Unsandboxed host worker** and is eligible only
  for manual read-only work; and
- manifest/schema compatibility, version drift, sanitization, and restart
  behavior have automated tests.

### Phase 0C: run control and manual read-only agent

Build:

- run/event/message/action/resource schema;
- dispatcher and startup reconciler;
- native `ClaudeRunner` over the structured stream protocol;
- one manual **Investigate** run pinned to a revision with read-only tools;
- run detail page, conversation, waiting-human question, message delivery,
  pause/interrupt/cancel/resume/follow-up controls;
- active-session list with process-tree memory, recent-message summary/tail,
  Send-guidance and confirmed Kill-session controls;
- triage/engineering execution profiles, qualifying-activity tracking,
  timeout termination, owner-scoped cleanup, immediate timeout email, and
  visible countdown/failure reason;
- two-hour engineering reminders and the nonextendable 48-hour absolute cap;
- structured worker report validation;
- clearly visible unsandboxed-worker label.

Exit criteria:

- one patch cannot obtain two active runs under race;
- human messages deliver exactly once and their state is visible;
- waiting-human survives service restart and resumes only after a valid answer;
- terminal/stale runs cannot be silently resumed;
- a live Claude runner session is adopted after Patch Watcher restart;
- simulated 20-minute triage runtime and 30-minute engineering inactivity each
  fail exactly once, email exactly once, and clean only run-owned resources;
- waiting-human/external and paused/blocked time does not consume an inactivity
  interval;
- reminder intervals survive restart without duplicate email, and the 48-hour
  cap cannot be extended;
- an email Kill-session link cannot mutate state through GET and reaches the
  authenticated confirmation flow;
- no external write capability is present.

### Phase 1: deterministic automatic retest

**Implementation status: complete.** The checked-in controller, Maloo adapter,
pure policy evaluator, background observer, durable automation ledger,
dashboard confirmations/timeline, and notification projections implement this
phase. External execution still defaults off globally and per patch.

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

Implemented:

- automatic or manual investigation trigger for failures without linked bugs;
- controller-captured, immutable Maloo evidence plus a pinned source checkout;
- evidence report, recommendation, and human escalation;
- runtime/turn budgets and bounded artifact/log display;
- prompt-injection defenses and controller-owned tool access.

The agent gets no Gerrit, Maloo, Jira, Jenkins, LTVM, shell, or file-write
tool. Identical evidence deduplicates across retries and restarts. Automatic
triggering has its own confirmed per-patch policy and budget and also respects
the global execution switch. Existing-Jira association and the subsequent
retest are a separate two-step operator-approved controller workflow: each
action is revision-pinned, independently confirmed, remotely reconciled, and
never retried after an ambiguous outcome.

Exit criteria:

- agent cannot retest, comment, vote, edit, upload, or access ungranted secrets;
- every factual recommendation links to captured evidence;
- malformed/missing reports block cleanly;
- human can message, redirect, or stop the run from the dashboard;
- a newer patchset stales the run before its recommendation can be acted on.

### Phase 3: isolated execution foundation

Phase 3 is deliberately split so source editing, execution, and publication do
not arrive as one oversized capability grant.

#### Phase 3A: private source-edit runs

Build:

- full independent checkout lifecycle;
- exact-revision, two-step operator confirmation before a source-edit worker
  starts;
- a source-edit Claude profile limited to `Read`, `Glob`, `Grep`, `Edit`, and
  `Write` inside the dedicated checkout, with no Bash, MCP, browser, service
  credentials, or Gerrit write capability;
- capture the actual Git diff/status independently of the agent and retain
  them as immutable, digest-addressed evidence;
- accept desired validation only as a bounded argv manifest. The request is
  inert until a later controller stage admits and executes it;
- dashboard progress, messages, exact revision, checkout ownership, evidence,
  and explicit **Gerrit upload disabled** status.

#### Phase 3B: session-owned LTVM validation

Build:

- consume LTVM's existing session-scoped `owner_id` inventory and implement
  reconciliation;
- inventory all current LTVM VMs, associate matching owner IDs beneath their
  sessions, and show configured guest memory separately from measured host
  process use;
- agent-driven, on-demand VM/cluster creation with target
  list/fetch/validate guidance and recorded VM environment;
- structured LTVM resource-exhaustion reporting, email, partial-resource
  cleanup, per-patch cooldown, and operator retry controls;
- safe command/test manifests rather than arbitrary dashboard shell text;
- artifact collection, cleanup, quarantine, and orphan reconciliation;
- rootless worker container prototype and network-profile display.

The controller, not the web request and not an untrusted repository file,
admits each manifest step. Build/test execution occurs in the selected
session-owned guest. No Phase 3 command runs inside the Patch Watcher web
service process.

#### Phase 3C: separately gated Gerrit upload

After 3A and 3B are proven, add a distinct upload capability. It remains
disabled by default and is never implied by permission to edit, build, or
test. Upload requires an exact-current-patchset recheck, a reviewable diff and
test evidence, a controller-generated upload plan, and explicit operator
approval. Ambiguous upload outcomes reconcile with Gerrit before any retry.
The uploaded patchset becomes a new observed revision and cannot silently
reuse the old run's authority.

Exit criteria:

- untrusted build/test code does not execute in the web service or host worker
  context;
- two runs cannot share a writable checkout or owner-attributed VM;
- cancellation and restart do not destroy unrelated VMs;
- environment and test results are reproducible from the run manifest;
- a simulated LTVM capacity failure creates no retry loop, purges only
  owner-matched partial resources, emails once, and suppresses that patch until
  cooldown or confirmed manual retry;
- restricted-egress behavior is tested and honestly represented.
- Phase 3A/3B completion does not enable upload; 3C has its own capability,
  confirmation, audit, and kill switch.

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

### Phase 5: Jenkins build handling and broader controlled Gerrit writes

Build:

- design and implement Jenkins failure classification separately from Maloo
  retesting;
- narrowly granted Gerrit replies/messages and reuse of the separately proven
  Phase 3C patchset-upload controller;
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

### Future parallel surface: ticket and free-form engineering

This work is intentionally not assigned to the patch-shepherding phases yet.
Before enabling it, design and build:

- an **Engineering work** page with separate Jira-ticket and free-form prompt
  start forms;
- a versioned work-item envelope that records input kind, immutable original
  input, submitter, selected project/repositories, source baselines,
  acceptance criteria, capability policy, budget, and approval policy;
- Jira issue retrieval and snapshotting, including instance identity and
  issue-version/change detection;
- an explicit discovery or human-confirmation step when a ticket does not
  unambiguously identify its repositories, branch, expected deliverable, or
  test environment;
- work-item concurrency rules and repository/patch write locks, since one
  ticket may create multiple patches and multiple tickets may mention the same
  repository;
- first-class outputs linking proposed commits, Gerrit changes, tests,
  artifacts, questions, and final outcome back to the originating work item;
- distinct prompt-injection boundaries for Jira content and free-form task
  text; and
- dashboard history and controls equivalent to patch runs: current state,
  recent messages, resource use, guidance, waiting-human questions, stop,
  retry, follow-up, and cleanup.

Initial acceptance should be read-only planning from a Jira ticket, followed
by an isolated, approval-gated implementation lane. Free-form engineering
should remain disabled until its required repository/environment selection
and capability controls are explicit and tested. Neither mode should inherit
automatic Gerrit, Jira, CI, or upload authority merely because the requested
task mentions such an action.

## Test strategy

Build a fake-adapter test harness before enabling actions. Required suites:

- table-driven evaluator and state-transition tests;
- SQLite migration, constraint, transaction, and crash-restart tests;
- duplicate poll/trigger/run/action races;
- stale patchset at every transition and immediately before writes;
- Claude runner lost process, idle turn, invalid marker/report, needs-input, interrupt, and
  resume behavior;
- triage wall-clock and engineering inactivity boundaries, qualifying versus
  irrelevant activity, suspended waiting-state clocks, one-time notification,
  stop escalation, and owner-scoped timeout cleanup;
- human message idempotency, stale question, queued delivery, and terminal-run
  follow-up behavior;
- external action success/failure/timeout/ambiguous/reconciliation;
- LTVM owner propagation, terminal purge, orphan, cleanup retry, retention, and
  unrelated-VM protection;
- host/session/VM memory attribution, process-tree accounting, stale samples,
  missing telemetry, no-double-count totals, and unassociated VM display;
- LTVM resource exhaustion versus ordinary create failure, email idempotency,
  partial-cluster cleanup, cooldown expiry, and manual override;
- two-hour reminder cadence across restarts, bounded message excerpts,
  authenticated kill-link behavior, confirmed human kill, and the 48-hour
  absolute cap;
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
