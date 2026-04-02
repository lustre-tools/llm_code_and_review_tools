# crash-tool

Non-interactive crash dump analysis CLI for LLM agents.
Structured JSON output from two complementary backends:

## Two Backends

**crash binary** (Red Hat crash utility) -- generic kernel
analysis. Backtraces, memory stats, process lists, block I/O
state. Good for "what happened to the kernel?" questions.

- `crash-tool run "bt -a" "log" --vmcore /path/to/vmcore`
- `crash-tool recipes overview --vmcore ... --vmlinux ...`

**drgn** (via lustre-drgn-tools) -- typed traversal of Lustre
kernel data structures. OBD devices, LDLM locks, dk log, RPC
queues, OSC grant stats, D-state analysis, diagnosis hints.
Good for "what happened to Lustre?" questions.

- `crash-tool recipes lustre --vmcore ... --vmlinux ... --mod-dir ...`

## When to Use Which

| Question | Backend | Command |
|----------|---------|---------|
| System overview, panic message | crash | `recipes overview` |
| All CPU backtraces | crash | `recipes backtrace` |
| Memory/slab stats | crash | `recipes memory` |
| Hung tasks, block I/O | crash | `recipes io` |
| Lustre full triage | drgn | `recipes lustre` |
| Ad-hoc crash commands | crash | `run "cmd1" "cmd2"` |

For Lustre problems, start with `recipes lustre` -- it runs
a comprehensive triage that covers OBD devices, locks, RPCs,
dk log, kernel log, stack traces, and diagnosis hints in one
shot. Fall back to crash recipes or `run` for generic kernel
analysis that drgn scripts don't cover.

## Recipes

```bash
crash-tool recipes                    # list available
crash-tool recipes overview           # generic kernel overview
crash-tool recipes lustre \           # Lustre triage (drgn)
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux \
    --mod-dir /path/to/lustre/build
```

## Ad-hoc Commands

```bash
crash-tool run "bt -a" "log" \
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux

crash-tool run --mod-dir /path/to/kos "sym obd_devs"

crash-tool script commands.txt --vmcore ...
```

## Install

```bash
pip install -e .
```

Requires the `crash` binary for non-drgn commands, and
`lustre-drgn-tools` (with drgn) for the lustre recipe.
