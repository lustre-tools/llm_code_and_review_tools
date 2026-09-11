---
name: lustre-crash-triage
description: This skill should be used to analyse a Lustre kernel crash or vmcore - "analyse this vmcore", "the node LBUGed", "what caused this panic", "triage this crash dump", "why did the client hang", "look at the crash in /var/crash", "run the lustre crash recipe", "find the stuck RPCs in this dump". Covers lustre-crash / crash-tool, the drgn scripts behind it, and the order that gets to an answer fastest.
version: 0.1.0
---

# Lustre crash triage

`lustre-crash` (installed as `crash-tool` too) runs non-interactive,
drgn-based analyses of a vmcore and returns structured JSON. It is the
tool to reach for after an LBUG, LASSERT, panic or oops.

## Read the dmesg first

The fastest answer is usually not a tool run. A crash directory contains
`vmcore-dmesg.txt`, which carries the LBUG or assertion line, the values
involved, and the backtrace:

```bash
ls /var/crash/                      # <ip>-<date>/ per crash
sed -n '/LBUG\|LASSERT\|BUG:\|Oops\|Call Trace/,$p' /var/crash/*/vmcore-dmesg.txt
```

For a plain assertion failure this is frequently the whole story: the
assertion names the condition, and the backtrace names the path. Go to
the vmcore only when the question needs state the log does not carry --
which locks were held, what RPCs were in flight, which tasks were stuck.

## Running a recipe

```bash
crash-tool recipes lustre \
    --vmcore /path/to/vmcore --vmlinux /path/to/vmlinux --mod-dir <build-tree>
```

Recipes: `overview` (system info, uptime, panic message, task summary),
`backtrace` (all CPU backtraces and the panic task), `memory`, `io`
(block devices and D-state tasks), and `lustre` (full triage, requires
`--mod-dir`).

`recipes lustre` returns system overview, backtrace with source lines,
OBD devices, LDLM namespace and lock summary, OSC grant/dirty stats,
in-flight RPCs, the dk log tail, kernel log, unique stack grouping,
D-state tasks and diagnosis hints. Start there for a Lustre problem and
`recipes overview` for a generic kernel one.

The only flags are `--vmlinux`, `--vmcore`, `--mod-dir`, `--timeout` and
`--pretty`. There is no `--minimal`.

**`--mod-dir` must be the build that crashed.** `git checkout` does not
rebuild, so the `.ko` files on disk are whatever was built last; pointing
at a different build yields symbol offsets that are quietly wrong.

## Going further than the recipes

The recipes are built from the scripts under `lustre-drgn-tools/` in the
tools checkout, and each answers one question directly. They take the
same `--vmcore`, `--vmlinux`, `--mod-dir` and `--pretty`:

`lustre_triage.py` (what `recipes lustre` wraps), `obd_devs.py`,
`ldlm_dumplocks.py`, `ldlm_deadlock.py`, `ptlrpc.py`, `dk.py`,
`lustre_waitq.py`, `osc_stats.py`.

```bash
python3 <tools-checkout>/lustre-drgn-tools/lustre_triage.py \
    --vmcore <path> --vmlinux <path> --mod-dir <build> --pretty
```

For a question none of them answers, drive drgn against the same dump:

```bash
drgn -c <vmcore> -s <vmlinux>
```

drgn must be installed **in the tools venv** that runs `lustre-crash`; a
standalone `drgn` on PATH built against the system Python does not
satisfy it:

```bash
<tools-checkout>/.venv/bin/pip install drgn
```

A recipe that answers `could not find 'init_uts_ns'` (or `'runqueues'`,
or `'PIDTYPE_PID'`) is not finding kernel debug info: the vmlinux does
not match the dump, or is stripped. Check the build-id rather than the
version string -- `ltvm vm crash-collect` compares them and says so.

For an ad-hoc query against the `crash` binary rather than drgn,
`lustre-crash run` takes crash commands and `lustre-crash script` takes a
file of them.

## Getting a vmcore in the first place

Test VMs are configured with kdump; the vmcore lands in
`/var/crash/<ip>-<date>/` after the reboot. Triggering and collecting is
`ltvm`'s job -- see the ltvm skill -- and `ltvm vm crash-collect` also
resolves the matching vmlinux itself and verifies its ELF build-id against
the running kernel, warning when it cannot find one. The debug kernel with
full DWARF lives in the ltvm artifacts tree.

Only go hunting for a vmlinux by hand when the collection step says it
could not find one. A mismatched vmlinux produces plausible, wrong
answers rather than an error.

## Reading the result

- **LBUG / LASSERT** -- the assertion text and its values are the finding.
  Search JIRA for the assertion string before assuming it is new; most
  have an LU already.
- **Deadlock or hang** -- `ldlm_deadlock.py` and the D-state task list;
  `lustre_waitq.py` shows what is waiting and on what. A node with many
  tasks in D state and no panic is a hang, not a crash.
- **Stuck IO** -- `ptlrpc.py` for in-flight RPCs and `osc_stats.py` for
  grant and dirty accounting.
- **Unique stack grouping** in the triage output collapses hundreds of
  tasks into the handful of distinct stacks that matter. Read that before
  reading individual backtraces.

For a hang on a live node rather than a dump, collect the Lustre debug
log instead. It is an in-kernel per-CPU ring buffer, ~5 MB/CPU by
default, and everything CDEBUG/CERROR writes lands there:

```bash
lctl set_param debug=-1
lctl set_param debug_mb=10000
lctl clear
lctl mark "before repro"
# ... reproduce ...
lctl dk /tmp/dk.log
```

`lctl` clamps `debug_mb` to its maximum, so asking for more than exists
is harmless.

## Reporting

Name the assertion or panic line verbatim, the task and stack it happened
on, and the state that explains it. Do not report a diagnosis that rests
on a vmlinux or `--mod-dir` that was not confirmed to match the crashing
build -- say so instead.
