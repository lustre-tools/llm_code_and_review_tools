# crash-tool

Non-interactive crash dump analysis CLI for LLM agents.
All recipes use drgn for structured, typed kernel analysis.

## Recipes

All recipes use drgn -- no crash binary needed.

```bash
crash-tool recipes                    # list available
crash-tool recipes overview \         # system info, dmesg
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux
crash-tool recipes backtrace ...      # CPU + panic backtraces
crash-tool recipes memory ...         # RAM + slab stats
crash-tool recipes io ...             # block devs + D-state tasks
crash-tool recipes lustre \           # full Lustre triage
    --vmcore /path/to/vmcore \
    --vmlinux /path/to/vmlinux \
    --mod-dir /path/to/lustre/build
```

| Recipe | What it does |
|--------|-------------|
| overview | System info, uptime, panic message, task summary, dmesg tail |
| backtrace | Per-CPU backtraces, panic task backtrace with source lines |
| memory | Total/free RAM, top 20 slab caches by size |
| io | Block device list, all D-state tasks with backtraces |
| lustre | Full Lustre triage: OBD devices, LDLM locks, dk log, RPCs, OSC stats, D-state analysis, diagnosis hints |

## Legacy: crash binary commands

The `run` and `script` subcommands send commands to the Red Hat
`crash` binary. These exist for ad-hoc queries where you already
know the crash command you want. The crash binary is not needed
for any recipe.

```bash
crash-tool run "bt -a" "log" --vmcore ... --vmlinux ...
crash-tool script commands.txt --vmcore ...
```

## Install

```bash
pip install -e .
```

Requires drgn and lustre-drgn-tools (for all recipes).
The crash binary is only needed for `run`/`script`.
