---
name: lustre-cscope-nav
description: This skill should be used for fast Lustre code navigation with cscope - "where is this function defined", "who calls this", "find all callers of this function", "what does this function call", "find this symbol", "search Lustre source for this string", "find files including this header", "the cscope index is stale", "build a cscope database", "which file has this symbol". Covers building/refreshing cscope.out per checkout (seconds, even under heavy load) and the non-interactive query cookbook.
version: 0.1.0
---

# Lustre cscope navigation

`cscope` is a plain tokenizer, not a compiler -- it doesn't run the
preprocessor, doesn't resolve headers, doesn't need a build. That's
exactly why it works here: indexing the whole Lustre tree takes about
two seconds even on a heavily loaded box, because it never pays the cost
a real AST tool (clangd, rtags) pays to parse a translation unit with
its full include chain. Reach for it before grep for anything
symbol-shaped -- definition, callers, callees, `#include` graph -- and
before a real compiler-based tool for anything else, since those are an
order of magnitude slower to index and load-sensitive on a shared box.

The trade-off for that speed: cscope matches identifier text, not
resolved symbols. A generic name (`type`, `len`, `lock`) returns every
occurrence regardless of which struct/function it actually belongs to,
and a call hidden behind a macro wrapper won't show up as a call to the
thing the macro expands to. Cross-check the actual definition when a
result looks off rather than trusting the match count.

## Building / refreshing the database

Per checkout, from the tree root:

```bash
cd <checkout> && cscope -R -b -q -k
```

- `-R` recurse into subdirectories
- `-b` build only, no interactive UI
- `-q` build the inverted index (faster symbol lookups; the build itself
  is still ~2s with it on)
- `-k` kernel mode -- don't pull in `/usr/include`

This writes `cscope.out` (plus `cscope.in.out`/`cscope.po.out` for the
inverted index) at the tree root -- already covered by Lustre's own
`.gitignore` (`cscope.*`), so nothing to exclude by hand. It is cheap
enough to just re-run before a navigation-heavy session, or whenever
you `cd` into a different `$CO/N` checkout -- the database is
per-directory and knows nothing about any other tree's.

## Querying

`-d` reads the existing database without triggering a rebuild; `-L`
does one non-interactive, line-oriented search and exits. The field
number is glued directly to the pattern, no space: `-L -3ldlm_lock_decref`,
not `-L -3 ldlm_lock_decref`.

| Field | Question | Example |
|---|---|---|
| `-0` | Find this C symbol (any reference) | `cscope -d -L -0ldlm_lock_decref` |
| `-1` | Find this global definition | `cscope -d -L -1ldlm_lock_decref` |
| `-2` | Find functions called by this function | `cscope -d -L -2ldlm_lock_decref` |
| `-3` | Find functions calling this function | `cscope -d -L -3ldlm_lock_decref` |
| `-4` | Find this text string | `cscope -d -L -4"lock is not granted"` |
| `-6` | Find this egrep pattern | `cscope -d -L -6'ldlm_.*_decref'` |
| `-7` | Find this file | `cscope -d -L -7ldlm_lock.c` |
| `-8` | Find files `#include`-ing this file | `cscope -d -L -8lustre_dlm.h` |
| `-9` | Find assignments to this symbol | `cscope -d -L -9obd_timeout` |

Output is one match per line: `file  context  line  text`, where
`context` is the enclosing function name or `<global>` outside any
function. `-1` is the sharpest tool for "where is this defined" -- it
returns only the actual definition, not every reference `-0` would also
include.

```
lustre/ldlm/ldlm_lock.c ldlm_lock_decref 931 void ldlm_lock_decref(const struct lustre_handle *lockh, enum ldlm_mode mode)
```

`-4` and `-6` are the plain-text and regex equivalents of grep, but
scoped to whatever `cscope -R` indexed -- useful when grep's noise
(build artifacts, `.git`, generated files) isn't wanted, though a plain
`grep -r` remains simpler for a one-off across directories cscope
wasn't pointed at.

## Working a problem

- **"Where is X defined"** -- `-1`, not `-0`: fewer, more precise hits.
- **"Who calls X"** -- `-3`. Check whether any hit sits inside a macro
  expansion before trusting a "no results" answer; cscope can't see
  through one.
- **"What does X call"** -- `-2`, one level only; walk it manually to go
  deeper.
- **"Who includes this header"** -- `-8`, useful before changing a
  widely-`#include`d header's ABI.
- **A result looks wrong or missing** -- rebuild (`cscope -R -b -q -k`);
  the database is a point-in-time snapshot, not live-updated the way an
  editor plugin might expect.
