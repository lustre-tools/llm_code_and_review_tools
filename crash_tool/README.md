# crash-tool

Non-interactive crash dump analysis CLI for LLM agents.
Structured JSON output.

## Two Backends

**drgn** (via lustre-drgn-tools) -- **preferred**. Programmatic
access to all kernel data structures with full type info. Has
built-in helpers for tasks, stacks, memory, slab caches, block
devices, dmesg, and more -- plus Lustre-specific analysis (OBD
devices, LDLM locks, dk log, RPC queues, OSC stats, D-state
analysis, diagnosis hints).

- `crash-tool recipes lustre --vmcore ... --vmlinux ... --mod-dir ...`

**crash binary** (Red Hat crash utility) -- **legacy**. The
overview/backtrace/memory/io recipes still use it, and `run`
lets you send arbitrary crash commands. Everything the crash
recipes do can also be done with drgn -- these recipes exist
for convenience if you already know crash commands.

- `crash-tool run "bt -a" "log" --vmcore /path/to/vmcore`
- `crash-tool recipes overview --vmcore ... --vmlinux ...`

## Usage

Start with `recipes lustre` for Lustre problems -- it runs a
comprehensive triage in one shot. The crash-binary recipes
remain available but are not required.

```bash
crash-tool recipes                    # list available
crash-tool recipes lustre \           # Lustre triage (drgn)
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux \
    --mod-dir /path/to/lustre/build
crash-tool recipes overview           # generic kernel (crash)
crash-tool run "bt -a" "log"          # ad-hoc crash commands
crash-tool script commands.txt        # commands from file
```

## Install

```bash
pip install -e .
```

Requires `lustre-drgn-tools` with drgn for the lustre recipe.
The crash binary is only needed for the legacy recipes and
`run`/`script` commands.
