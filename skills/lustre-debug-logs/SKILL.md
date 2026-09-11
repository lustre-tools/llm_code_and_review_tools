---
name: lustre-debug-logs
description: This skill should be used to capture and read Lustre's in-kernel debug log - "turn on Lustre debug logging", "collect a dk log", "what do these debug masks mean", "enable dlmtrace", "trace this RPC", "follow this xid", "filter this dk log", "which subsystem should I enable", "the log wrapped before I could catch it", "why is this operation stuck". Covers lctl debug masks, the capture ritual, the dk line format, and dk-filter.
version: 0.1.0
---

# Lustre debug logs

Lustre writes CDEBUG/CERROR output to an in-kernel ring buffer, per CPU,
about 5 MB per CPU by default. It wraps. Everything below is about
getting the right few thousand lines into a file before that happens,
and reading them afterwards.

For a node that panicked rather than one that is merely misbehaving, use
the crash triage skill instead -- the dump carries its own dk tail.

## The capture

```bash
lctl set_param debug=-1          # every mask
lctl set_param debug_mb=10000    # buffer per CPU; lctl clamps to its max
lctl clear                       # start empty -- this is what makes the
                                 # log begin at the reproduction
lctl mark "before repro"
# ... reproduce ...
lctl dk /tmp/dk.log
```

`lctl clear` immediately before the reproduction is the step that makes a
log readable. Without it the interesting lines arrive at the end of
megabytes of unrelated history, and on a busy system they may have pushed
the start of the reproduction out of the buffer entirely.

`lctl mark <text>` writes a marker line, and markers are how a filter
later picks out exactly the window that matters. Mark both sides of
anything slow.

Logs are per node. A client-side symptom with a server-side cause needs
the log from both, captured over the same window.

## Choosing masks

`debug` selects message types, `subsystem_debug` selects which subsystems
may log at all. Both take the same operators:

| Form | Meaning |
|---|---|
| `debug=cache` | exactly this |
| `debug=+cache` | add to what is set |
| `debug=-cache` | remove |
| `debug=-1` | everything |
| `debug=0` | the minimum |

Recipes that answer most questions:

```bash
lctl set_param debug=dlmtrace subsystem_debug=ldlm                  # lock contention
lctl set_param debug=vfstrace+iotrace+page subsystem_debug="llite osc lov"   # IO path
lctl set_param debug=rpctrace+net subsystem_debug="rpc osc mdc"     # RPCs on the wire
lctl set_param debug=ha+net+rpctrace+config subsystem_debug=-1      # recovery / eviction
```

Restore the normal working set when finished:

```bash
lctl set_param debug="vfstrace rpctrace dlmtrace neterror ha config ioctl super lfsck"
```

`debug=-1` on a busy filesystem produces enormous volume and wraps
quickly. Reach for it when the reproduction is fast or the cause is
unknown; narrow to a recipe above once the area is known.

`lctl debug_list types` and `lctl debug_list subs` print the authoritative
names -- 30-odd masks (trace, inode, super, iotrace, malloc, cache,
dlmtrace, rpctrace, vfstrace, ha, quota, sec, lfsck, layout, ...) and the
subsystems (mdc, mds, osc, ost, llite, rpc, lnet, ldlm, lov, lmv, osd,
mgc, mgs, fid, fld, ...).

## Inside the test framework

`cfg/local.sh` sets `PTLDEBUG` to `vfstrace rpctrace dlmtrace neterror ha
config ioctl super lfsck` with `SUBSYSTEM=all`, so a test run is already
logging usefully. Around a specific test:

- `debugsave` / `debugrestore` -- save and put back the mask
- `debug_size_save` / `debug_size_restore` -- same for the buffer size
- `start_full_debug_logging` / `stop_full_debug_logging` -- `debug=-1`
  with `debug_mb=150`

Use the save/restore pairs rather than setting masks by hand in a test:
a test that leaves the mask changed corrupts every test after it.

## Reading a dk log

Each line is colon-separated:

```
SUBSYS:MASK:CPU.TYPE[F]:SEC.USEC:STACK:PID:EPID:(FILE:LINE:FUNC()) TEXT
00000080:00200000:0.0F:1774820204.815885:0:8014:0:(file.c:6208:ll_inode_revalidate()) VFS Op:...
```

- SUBSYS and MASK are 8-hex bitmasks -- `00000080` llite, `00000008` osc,
  `00010000` ldlm, `00020000` lov, `00000100` rpc; `00200000` vfstrace,
  `00100000` rpctrace, `00010000` dlmtrace, `00020000` error
- CPU.TYPE is the CPU plus context: 0 process, 1 softirq, 2 irq; a
  trailing `F` marks the first line on that CPU
- SEC.USEC is a unix timestamp -- the field to sort and window on
- PID is the kernel task

Time ordering across CPUs is not the order lines appear in the file.
Sort on SEC.USEC before concluding anything about sequence.

## dk-filter

`dk-filter` is an awk script that filters on those fields. It ships with
ltvm (`ltvm_pkg/dk-filter`), which `ltvm install` puts at
`/usr/local/bin/dk-filter`; on a host without ltvm, `grep` on the hex
fields does the same job less conveniently.

```bash
dk-filter -v fpid=1234 /tmp/dk.log             # one task
dk-filter -v fsub=00000080 /tmp/dk.log         # one subsystem (llite)
dk-filter -v fmask=00200000 /tmp/dk.log        # one mask (vfstrace)
dk-filter -v ffunc=ll_file /tmp/dk.log         # function name, regex
dk-filter -v ffile=osc_request /tmp/dk.log     # source file, regex
dk-filter -v ftext='No space' /tmp/dk.log      # message text, regex
dk-filter -v xid=1861033837809152 /tmp/dk.log  # one RPC, both ends
dk-filter -v marker=repro /tmp/dk.log          # between `lctl mark` markers
dk-filter -v after=SEC.USEC -v before=SEC.USEC /tmp/dk.log
dk-filter -v fsub=00000080 -v invert=1 /tmp/dk.log
```

Filters combine, and `lctl dk | dk-filter ...` works as a pipe.

`xid` is the one to remember: an RPC's xid appears in both the client and
server logs, so filtering both captures on the same xid gives the two
halves of one request.

## Working a problem

- **Stuck or slow IO** -- `rpctrace+net` on `osc mdc rpc`, then find the
  oldest in-flight xid and follow it into the server log.
- **Lock contention or an eviction** -- `dlmtrace` on `ldlm`; look for
  the lock's resource id, then everything touching that resource.
- **Something at the VFS boundary** -- `vfstrace` on `llite` names the
  operation and inode before the stack disappears into the OSC layer.
- **Recovery** -- `ha+net+rpctrace+config` with all subsystems, on both
  ends; the interesting lines are usually on the server.

Quote what the log actually says -- the function, the timestamp, the
values -- rather than summarising it. A debug log is evidence, and the
line numbers in `(FILE:LINE:FUNC())` point straight at the source.
