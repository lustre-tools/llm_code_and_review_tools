# Patch Watcher Worker Environment Contract

This document defines the portable execution environment for a Patch Watcher
AI engineering run. It answers a question that the orchestration design
previously left implicit: **what may Claude assume exists when Patch Watcher
starts an engineer, and how can that engineer be reproduced on another
machine?**

The current development machine is a useful reference implementation, not the
contract. A worker must not depend on Patrick's home directory, shell startup
files, ambient credentials, or a particular checkout layout.

## Three distinct environments

The phrase "engineer in a box" covers three boundaries that must remain
separate:

1. **Patch Watcher controller.** Owns workflow state, policy, secrets,
   external side effects, scheduling, notifications, and the audit trail.
2. **AI worker environment.** Runs Claude Code against one revision-pinned
   checkout. It receives a narrow capability grant, run inputs, scratch space,
   and report channel.
3. **LTVM test guests.** Disposable machines or clusters selected by an
   engineering worker for builds and tests. They are subordinate session
   resources, not the Claude sandbox.

The **worker host** is the physical or virtual machine that supplies the Claude
runner, resource sampler, and host services such as the LTVM bridge. Moving an
engineer means recreating an admitted worker environment on a compatible
worker host. It does not mean copying a user's home directory or baking LTVM
guest images into the worker image.

## Portability invariant

A worker may assume only facts declared by its admitted worker profile and run
envelope. Anything discovered merely because it happens to be on `PATH`, in
`HOME`, or reachable on the current network is ambient state and must not be a
hidden dependency.

Patch Watcher must perform environment admission before Claude starts. A
missing or incompatible dependency blocks dispatch with a precise reason; it
must not become a surprise halfway through an agent turn or cause a silent
fallback to a more privileged environment.

## Versioned contract objects

Every agent-backed run is described by three separately versioned objects.

### Worker profile

A `WorkerProfile` is checked in, reviewed, and immutable for a given version.
It declares:

- profile schema, ID, version, and content hash;
- supported host OS and architecture;
- Claude Code, Node, Python, Git, and worker-protocol version constraints;
- required commands and the JSON API/schema versions of domain tools;
- permitted Patch Watcher capabilities;
- isolation and network profiles;
- logical filesystem layout and mount modes;
- required host services, such as the Claude runner, tool broker, resource
  sampler, artifact collector, and optional LTVM bridge;
- default CPU, memory, disk, process, runtime, and output limits; and
- whether ambient home-directory state is permitted.

Profiles describe capability, not a particular patch. Example profile names:

- `host-unsandboxed-mac-v1` for the current visibly legacy environment;
- `native-triage-v1` for a reproducibly bootstrapped read-only worker;
- `container-standard-v1` for isolated engineering work; and
- `container-restricted-v1` for later restricted-egress work.

### Run envelope

Patch Watcher generates a private `RunEnvelope` for one run. It contains:

- run, change, patchset, and exact revision SHA;
- worker profile ID and expected profile hash;
- task and snapshotted capability grant;
- checkout mode and logical paths;
- runtime, inactivity, resource, action, and output budgets;
- isolation and network mode;
- controller, broker, heartbeat, report, and artifact endpoints;
- a durable `LTVM_OWNER_ID` when LTVM is granted;
- references and hashes for the evidence bundle;
- artifact collection and retention policy; and
- a hash of the generated worker instructions.

The envelope is stored mode `0600` in the first host implementation. A later
remote-worker implementation signs or authenticates it. A worker cannot widen
its envelope, and Patch Watcher must reject reports or action requests whose
run, revision, profile hash, or capability does not match it.

### Environment attestation

`pw-worker doctor` produces an `EnvironmentAttestation` at admission. It
records what is actually present:

- OS, architecture, worker-host identity, and image digest or host build ID;
- resolved executable paths and versions;
- Claude Code, Node, Python, Git, LLM-tools, and LTVM versions/commits;
- Claude runner and worker-report protocol versions;
- broker, report channel, artifact channel, and optional LTVM health;
- config schema compatibility without credential values;
- checkout revision, cleanliness, mount modes, and free space;
- active isolation/network profile and resource limits;
- warnings, deviations, and unavailable optional capabilities; and
- worker-profile and run-envelope hashes.

Patch Watcher stores the attestation with the run. An active run never receives
an in-place tool update: version changes apply only to newly admitted runs.

## What lives in the worker box

### Common runtime

All worker profiles include:

- Claude Code and a pinned compatible Node runtime;
- Git, an SSH client where required, and basic POSIX utilities;
- `rg`, `jq`, and a pinned Python virtual environment;
- the Patch Watcher `pw-worker` shim; and
- one generated instruction bundle and report protocol.

`pw-worker` provides machine-readable commands for:

- `doctor` and runtime attestation;
- heartbeat and qualifying-activity reports;
- structured turn/final reports;
- artifact registration; and
- typed controller action or human-decision requests.

### Domain tools

The initial Lustre-oriented profiles may expose broker clients or safe
read-only forms of:

- Gerrit (`gc`/`gerrit`);
- JIRA (`jira`);
- Maloo (`maloo`);
- Jenkins (`jenkins`);
- Janitor (`janitor`);
- GitHub (`gh`) when a run needs TLC repository context;
- the Patch Watcher LTVM broker only for an engineering profile granted
  `start_ltvm` and `run_vm_tests`; and
- optional specialist profiles such as crash analysis or AI review tooling.

Tool presence is not enough. The profile pins the supported machine-readable
API and exit-code contract, and `doctor` smoke-tests it without causing an
external write. `lreview`, crash tooling, compilers, and other large or
specialized dependencies are optional profile features, not universal worker
assumptions.

For a manually confirmed engineering run, those two grants form one
open-ended guest capability: Claude may choose arbitrary build, test, and
diagnostic commands inside exactly owner-matched LTVM guests. This is not an
argv allowlist and does not require per-command approval. The broker owns host
operations, revalidates the owner before each guest command, and records the
command and bounded result. Untrusted patch builds and tests never run directly
in an unsandboxed host worker.

### What does not live in the worker box

The worker does not contain or mount:

- the Patch Watcher database;
- the controller's full credential/config directory;
- the host user's home directory or shell startup files;
- an SSH agent, Docker/Podman socket, or unrelated source checkouts;
- QEMU, dnsmasq, privileged host bridge configuration, or LTVM target storage;
- sendmail configuration or notification state; or
- authority to perform controller-owned Gerrit, CI, JIRA, or upload writes.

These remain controller or worker-host services.

## Logical filesystem contract

Worker instructions use logical paths, not `/Users/patrick/...` or another
host-specific location:

```text
/work/source                         dedicated full checkout
/work/input                          read-only evidence bundle
/work/scratch                        disposable writable work
/work/output/artifacts               registered artifacts
/work/output/logs                    bounded worker/command logs
/work/output/worker-report.json      structured report
/run/patch-watcher/run-envelope.json read-only private envelope
/run/patch-watcher/broker.sock       capability broker endpoint
```

Native-host profiles map those logical paths into a private per-run root.
Containers use them directly. `HOME` and XDG directories point at a private
per-run home; no tool may rely on the operator's dotfiles. The checkout is
read-only for triage and writable only for profiles granted source editing.

## Controller-provided inputs

Before admission, Patch Watcher prepares:

- a clean, full checkout at the exact revision;
- a bounded, immutable evidence bundle;
- the run envelope;
- generated `WORKER_INSTRUCTIONS.md`;
- capability-scoped broker access;
- model transport/authentication;
- output, scratch, and artifact directories; and
- the session owner identifier and a private, exact-owner LTVM broker when
  granted. The broker exposes VM lifecycle, source transfer, and open-ended
  guest execution, but no general host shell or controller-owned service
  credentials.

The first LTVM SSH bridge checks exact owner inventory immediately before
dispatch and uses only the literal inventory address with ambient SSH config,
proxies, forwarding, and local commands disabled. Because current LTVM has no
atomic owner-checked exec operation, the check and dispatch remain adjacent;
the portable contract calls for an LTVM RPC that combines them. The initial
credential/egress properties are declared provisioning facts, not yet LTVM
attestation fields, and must become machine-verifiable before they are treated
as a portable isolation guarantee.

Worker instructions are assembled from versioned Patch Watcher policy,
portable organization policy derived from the useful parts of the user-level
`AGENTS.md`, relevant repository instructions, the run task, and the capability
grant. Patch Watcher snapshots and hashes the final text.

The user-level `AGENTS.md` is source material for the first portable policy
template; it is not itself a runtime dependency. Repository, Gerrit, JIRA, CI,
log, source, and web content remain untrusted inputs and cannot redefine the
controller's policy or capability grant.

## Worker assumptions and prohibitions

After successful admission, a worker may assume:

- the declared binaries and API schemas are present;
- its logical paths have the declared permissions;
- granted broker operations are reachable;
- model transport and the report channel are healthy; and
- declared worker-host capabilities are currently available.

It may not assume:

- ambient credentials, `sudo`, arbitrary internet access, or an SSH agent;
- a particular home directory, repository path, branch, locale, or shell;
- a pre-existing VM, LTVM target, reference checkout, or free host capacity;
- that email or external write access is available directly; or
- that another run's writable state or resources may be inspected or changed.

## Credentials and action enforcement

Model authentication belongs to the Claude runner. External write credentials
remain controller-side behind typed, policy-checked broker actions. If a worker
must receive a read credential, inject an expiring profile-scoped secret under
a private runtime secrets directory; never copy the operator's original
configuration file.

Withholding a command name is not a security boundary. Several current CLIs
combine read and write operations, and an unsandboxed shell with ambient
credentials could call their write paths directly. The same is true of LTVM:
`owner_id` provides attribution and cleanup identity, but does not by itself
prevent `ltvm destroy OTHER_NAME`.

The later `ltvm-bridge` must therefore:

- inject the current run's owner ID;
- expose only granted verbs;
- validate names and arguments;
- require an exact owner match for destructive operations; and
- emit an auditable typed result.

Until those boundaries exist, direct host-tool access is explicitly labeled
**trusted unsandboxed** and is not eligible for untrusted execution or an
autonomous lane.

## Worker-host contract

An admitted worker host advertises:

- a stable host ID, OS, architecture, and available worker profiles;
- current CPU, memory, disk, process, and worker-slot capacity;
- Claude runner and resource-sampler health;
- broker/API versions;
- LTVM version, virtualization mode, target inventory, and target hashes;
- host privilege-boundary health; and
- a heartbeat and last-admission time.

LTVM targets remain dynamic host capabilities. A run requests requirements
such as architecture, kernel/page-size constraints, and topology; it does not
assume a target is baked into the worker image. The worker may select among
admitted targets or request a controller-visible target operation according to
policy.

The first deployment has one host. Persisting this contract now prevents the
database and UI from assuming there can only ever be Patrick's Mac.

## Admission and `doctor`

The planned command is:

```text
pw-worker doctor --profile PROFILE --run-envelope PATH --json
```

It must verify, without exposing secrets:

1. profile schema and content hash;
2. OS, architecture, runtime, and tool compatibility;
3. exact checkout revision and clean initial state;
4. required paths, mount modes, privacy, and free disk/memory headroom;
5. runner, heartbeat, report, artifact, and broker connectivity;
6. credential availability and expiry by opaque reference;
7. tool JSON API smoke tests;
8. LTVM/bridge health and owner propagation for VM-capable profiles;
9. clock, CA, DNS, and declared network policy; and
10. absence of forbidden mounts, sockets, and sensitive environment values.

Admission is a durable run step before `preparing`. A failed check records the
attestation and blocks dispatch; Claude is not started.

## Failure taxonomy

Environment admission and runtime failures use precise codes:

- `profile_unknown`, `profile_hash_mismatch`, `unsupported_platform`;
- `bootstrap_failed`, `tool_missing`, `tool_version_mismatch`;
- `runner_unavailable`, `model_auth_unavailable`;
- `credential_unavailable`, `credential_expired`;
- `broker_unreachable`, `network_policy_denied`;
- `checkout_revision_mismatch`, `checkout_dirty`;
- `insufficient_disk`, `insufficient_memory`;
- `ltvm_unavailable`, `ltvm_target_unavailable`,
  `ltvm_resource_exhausted`;
- `report_channel_failed`, `artifact_collection_failed`; and
- `cleanup_failed`.

Pre-start failures leave the trigger queued or the run visibly blocked with
the exact reason. Post-start failures use the existing `blocked`, `failed`, or
`resource_exhausted` state with one of these codes; they do not require a new
collection of vague top-level states.

## Reproducibility and provenance

Every run retains:

- profile ID/hash and environment/image digest;
- complete environment attestation;
- source revision and initial checkout-state hash;
- Claude Code, Node, Python, LLM-tool, and LTVM versions/commits;
- instruction and evidence hashes;
- isolation/network profile and capability grant;
- target, kernel, architecture, page size, and artifact checksums where used;
- resource limits and observed resource summary; and
- output/artifact hashes.

This is sufficient to explain the environment and attempt a later reproduction
without claiming that a mutable developer account is reproducible.

## Phased delivery

### A. Admit the current host honestly

Create `host-unsandboxed-mac-v1`, a generated run envelope/instruction bundle,
fixed per-run directories, and `pw-worker doctor`. Record exact paths,
versions, and deviations. The dashboard displays **Unsandboxed host worker**.
Only manual, read-only investigation is eligible.

Exit test: a deliberately missing tool, incompatible Python, bad checkout, or
missing credential blocks Claude before process start with the correct visible
reason.

### B. Reproducible native worker

Bootstrap a dedicated worker user/prefix and pinned virtual environment from
checked-in locks. Remove dependencies on Patrick-specific paths and dotfiles.
Add fake-broker integration tests and prove the profile under a second local
account or clean host.

Exit test: two clean installations produce compatible attestations and can run
the same read-only evidence task without ambient home-directory state.

### C. Standard isolated container

Build a rootless, non-root OCI image pinned by digest, with a read-only base,
only per-run directories mounted, explicit resource limits, standard egress,
and narrow host tool/LTVM brokers.

Exit test: untrusted code cannot access the host home, credentials, unrelated
processes, or resources; owner-scoped LTVM cleanup still works. This is the
gate for broad code execution.

### D. Restricted and movable workers

Add model-only allowlisted egress or a separated no-network tool sandbox,
expiring capability tokens, multi-architecture images, remote worker-host
registration, host admission, and scheduling by declared requirements.

Exit test: Patch Watcher can move a run to another compatible admitted host
based on profile/capacity without changing task instructions or granting wider
authority. Only after this phase should autonomous lanes be considered.

## Immediate implementation slice

Before the native Claude runner performs meaningful work, implement:

1. JSON schemas for `WorkerProfile`, `RunEnvelope`, and
   `EnvironmentAttestation`;
2. `host-unsandboxed-mac-v1` based on a redacted inventory of the current host;
3. per-run logical directory mapping and generated portable instructions;
4. `pw-worker doctor` with unit tests for missing tools, version drift, checkout
   mismatch, insufficient resources, broker/report health, and sanitized
   output;
5. run/session persistence for profile/hash, environment instance,
   attestation, instruction hash, and broker session ID; and
6. dashboard admission state, isolation/network badge, provenance, warnings,
   and failed-preflight reason.

The next slice then exercises that foundation with one manual, revision-pinned,
read-only Claude investigation run. Automatic retesting and write actions stay
disabled until their later phases.

## Current-host observations

A redacted inventory on 2026-08-30 found:

- Darwin arm64/macOS 26.6.2;
- Claude Code 2.1.251 and Node 25.6;
- Apple `/usr/bin/python3` 3.9.6 alongside pyenv Python 3.12.13;
- LLM tools resolved through user-specific pyenv shims at repository commit
  `05cc0df`;
- LTVM 0.20 at repository commit `01146e5`; and
- `lreview` documented in the repository but not installed on the current
  `PATH`.

Those are observations, not permanent requirements. The Python/path mismatch
and missing documented command already demonstrate why admission and
provenance must replace informal assumptions.
