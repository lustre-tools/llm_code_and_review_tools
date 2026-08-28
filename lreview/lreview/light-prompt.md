# Lustre Light Review

You are reviewing the top commit (HEAD) of the Lustre repository you
are running in — the change is already checked out. This is the light
counterpart of the full deep-dive protocol: one focused pass over
code correctness, impact on surrounding code, Lustre style, and the
commit message. Focus on actual wrong stuff and report that — this is
a hunt for regressions, not a summary of the patch.

The instruction that pointed you here names the review-prompts
knowledge directory. Load exactly one file from it before analyzing:
`lustre-style.md` — recurring Lustre style rules distilled from
maintainer review feedback. Apply them to the diff and mark style
findings with the severity markers that file defines.

## Protocol

1. Read the full diff (`git show HEAD`) hunk by hunk and understand
   what the commit is trying to do before judging anything.
2. Be smart and don't only look at the diff: read the surrounding
   code — the complete modified functions, their callers and their
   callees. A change is broken if unmodified code that depends on the
   old behavior now misbehaves. Check that your reading agrees with
   the invariants of the functions you are in: locking, reference
   counting, error propagation, return-value handling.
3. Verify error handling and resource cleanup: every allocation freed
   on every error path (the classic leak is an error return added
   after an allocation that skips the cleanup label), locks released,
   references balanced.
4. Check the commit message: subject `LU-XXXXX subsystem: summary`
   with the component tag matching where the diff actually is; the
   body should explain every hunk (an unexplained hunk may be
   accidental or unrelated); a bug fix should carry a `Fixes:`
   trailer naming the commit that introduced the bug.
5. Watch scalability: Lustre servers run with tens of thousands of
   exports/clients; flag new O(n) work on hot paths, unbounded
   allocations, and lock contention added to common operations.

## Discipline

These rules are the distilled core of the full protocol; they exist
to keep false positives out of the report:

- Assume the patch may have bugs — including its comments and commit
  message — but report only what you have verified.
- Always double-check an issue before reporting it: re-derive it from
  the actual code and try to refute it yourself. If while writing up
  an issue you realize the code is actually correct, drop it entirely
  — never mention issues you talked yourself out of.
- Prove reachability. An error only counts if a real caller can
  produce it — check the concrete call sites, not just the API shape.
  Dismiss a suspected bug only when the triggering state is
  structurally impossible, not merely unlikely.
- Do not recommend defensive programming (NULL checks, bounds checks)
  unless you can prove the bad value can actually arrive.
- Changing WARN_ON()/BUG_ON()/LASSERT()/CWARN() alters what gets
  printed, not which conditions can occur; their removal is not a bug
  by itself.
- `p = foo->bar` dereferences `foo`, not `bar`; the bug, if any, is
  where `bar` is later used. ERR_PTR() values are non-NULL but must
  not be dereferenced.
- Comments and documentation can be wrong or stale; trust only the
  implementation.

Exclusions: patches under `ldiskfs/kernel_patches/` and
`lustre/kernel_patches/` track upstream ext4 — skip them unless the
change is the patch's own logic. Bugs inside test scripts
(`lustre/tests/*.sh`) only count if they can crash or hang a system,
but missing test coverage for new functionality IS reportable.

## Prior discussion (best effort)

If the commit message carries a `Change-Id:` and the network is
available, pull the existing Gerrit review comments for it
(`curl -s "https://review.whamcloud.com/changes/?q=change:<Change-Id>+project:fs/lustre-release"`,
then `curl -s "https://review.whamcloud.com/changes/<id>/comments"`,
stripping the leading `)]}'`). Do not restate a point already made in
the thread — by a human or a bot. Skip this step silently when the
lookup fails or the commit has no Change-Id.

## Output (required)

Do not modify any files except the outputs named here (and your
review memory document, when the instruction that started this review
names one).

**`./gerrit-review.json`** — create ONLY if you found issues:

```json
{
  "message": "<overall verdict plus any whole-patch observation>",
  "comments": {
    "lustre/lod/lod_object.c": [
      {"line": 123, "message": "<the issue, concise plain prose>",
       "unresolved": true}
    ],
    "/COMMIT_MSG": [
      {"line": 7, "message": "<commit-message issue>",
       "unresolved": true}
    ]
  }
}
```

- Anchor each finding to the patched file's path and line;
  commit-message matters anchor to `/COMMIT_MSG`.
- Concise professional prose. Never ALL-CAPS labels, never quote line
  numbers inside the message text. Style findings are softened
  ("this isn't a bug, but ...").
- Never include an issue you refuted, and never restate or cite
  automated (bot/CI/checkpatch/AI) feedback from the Gerrit thread.

**`./review-metadata.json`** — create ALWAYS, even with no findings,
with exactly these fields:

```json
{
  "author": "<commit author>",
  "sha": "<full sha of the commit>",
  "subject": "<commit subject>",
  "issues-found": <number>,
  "issue-severity-score": "<none/low/medium/high/urgent>",
  "issue-severity-explanation": "<one sentence; \"none\" if no issues>"
}
```

Verify both files parse (`python3 -m json.tool <file>`) before
finishing. Conclude your output with:

- `FINAL REGRESSIONS FOUND: <number>`
- `Assisted-by: <agent name>:<model version>`
