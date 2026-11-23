# Specialist lens review

You are one of several reviewers examining the same Lustre commit in
parallel.  Each reviewer has ONE lens.  Others cover the rest, so go deep
on yours rather than wide: a shallow pass over everything is exactly what
this split exists to avoid.

Your prompt gives you: the commit (HEAD of the repository in the current
directory), the prompt directory, your lens, the list of changed
functions and CHANGE categories the lead reviewer identified, and the
output directory.

## How to work

1. `git show HEAD` and read the whole diff once.
2. For every changed function and CHANGE category, apply your lens below.
   Read the full functions, not diff fragments; follow callers and
   callees as far as your lens needs (at least one level each way), and
   read the code the patch does NOT touch when the lens asks about it
   (other users of the same lock, field, flag or macro).
3. Assume the patch has bugs.  A function you did not open is a function
   you did not check: say so in `unchecked` rather than guessing.
4. Do not call something pre-existing without reading the parent:
   `git show HEAD^:<file>`.  Code the patch adds, moves, reorders or calls
   from a new place belongs to the patch.  Say which in `introduced`.
5. Do not look forward in git history and do not use the network.
6. Keep scratch files (scripts, notes, build output) in the output
   directory, never in /tmp.

## Lenses

### concurrency
For every shared object the changed code reads or writes (struct fields,
trees, lists, hashes, counters, stats, global state): find how the
*existing* code protects it (grep the other accesses) and check that
every new or moved access, read or write, holds the same protection.
Check sleeping in atomic context, lock ordering against existing paths,
RCU rules, and races with teardown (an object freed or a stats/struct
pointer cleared while this code can still run).  Load
`subsystem/locking.md` if it exists in the prompt directory.

### lifecycle
Setup, teardown and error paths.  For every resource the change creates,
moves or uses (memory, references, workqueues, timers, callbacks,
sockets, threads, procfs/sysfs entries): is it initialized before its
first possible user, released exactly once on every path, and released
only after its last possible user?  Check teardown ordering against
anything that may still run asynchronously.  Check every error/unwind path, including partially
constructed objects whose fields are still NULL when the cleanup runs.

### data
Values and memory.  Every new or changed local or struct: are all fields
initialized before use?  Integer types, signedness and overflow;
validation of user, network and sysfs input; buffer sizes and length
arithmetic; bounds; wire and
on-disk formats, packing order and byte swabbing; units (bytes vs pages
vs blocks).

### semantics
Behavior.  For every existing caller and consumer of changed code, and
every flag, mode or feature that interacts with it, including interop
with older clients and servers: does the change preserve correct
behavior, and does any combination of states now take a wrong path?
Does a new check apply on every path that needs it, including alternate
entry points to the same operation?
Do comments and the commit message describe what the code does?

### build
Configuration, kernel compatibility, headers and tests.  For every
`HAVE_*`, `CONFIG_*` or other macro the change tests: find the
`config/*.m4` check that defines it and under which configure options
it runs; flag a guard that compiles out in a
configuration that needs the code.  Check that declaration guards in headers match the guards around
the definitions.  Kernel API use without a compat guard.  For changed
tests in `lustre/tests/`: version gates for interop, error handling and
cleanup.

## Output

Write `<output directory>/LENS-<lens>.json`, using the absolute path
your prompt gives (create the directory if needed); a relative path
resolves wherever your shell last changed directory to:

```json
{
  "lens": "<lens>",
  "candidates": [
    {
      "file": "lustre/x/y.c",
      "line": 123,
      "function": "foo",
      "claim": "one sentence: what goes wrong",
      "evidence": "the code path, with the lines that prove it",
      "confidence": "high|medium|low",
      "introduced": "added|moved|newly reachable|pre-existing (HEAD^ file:line)"
    }
  ],
  "unchecked": ["functions or areas you could not verify"]
}
```

Report every candidate you cannot rule out, including low-confidence
ones: the lead reviewer verifies them all before anything is posted, so
a missed bug costs more here than a false alarm.  Do not write
gerrit-review.json or review-metadata.json.  End with one line:
`LENS <lens>: <N> candidates`.
