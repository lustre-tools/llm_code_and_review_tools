# Observations

A running log. Newest round first. Each round says what was run, what
the numbers were, and what we concluded -- including the conclusions
that later rounds should try to knock down.

Numbers come from `./compare.py`; the reasoning does not, so it is
written down here rather than left to be re-derived.

---

## Round 1 -- 2026-09-09: gpt-5.6-sol medium vs opus xhigh

**Question.** Codex reaches the GPT-6 and GPT-5.6 families now. Is
gpt-5.6-sol at medium effort good enough to replace opus for patch
review, given it should be substantially cheaper and faster?

**Setup.** Four open Lustre changes the Gerrit `aireview` bot had
already reviewed, pinned to the patchset it commented on: 68339 ps2
(ec, +186/-39), 67980 ps2 (llite statahead, +431/-128), 68627 ps1
(utils SI units, +402/-88), 68468 ps1 (ptlrpc gss, +97/-32). Full mode,
`--jobs 4`, identical prompts.

| | aireview | opus xhigh | gpt-5.6-sol medium |
|---|---|---|---|
| findings | 22 | 10 | 4 |
| defect-class | 2 | 5 | 1 |
| mean wall clock | n/a | 16m06s | 8m06s |
| mean tokens | n/a | 5.9M | 2.4M |
| total cost | n/a | $26.41 | no figure (ChatGPT plan) |
| tool calls / review | n/a | 52-70 | 27-36 |

### The reviewers barely overlap

This is the result that matters, and it was not the question we asked.
Across 36 findings from three reviewers on byte-identical code there
was **essentially one partial overlap** -- opus at `statahead.c:2330`
and aireview at `:2352`, both about `reset:` doing `*pid = 0`, arguing
different consequences.

Opus overlaps aireview about as poorly as Sol does. So Sol's 4-vs-22 is
not "5x worse recall against a fixed truth"; these reviews are just not
reproducible. Local history says the same thing: the LU-19895 DIO patch
reviewed five times by opus produced 6, 5, 4, 6 and 3 findings.

**Consequence for this dataset:** do not read one run's finding count
as a model's score. Prefer "did it find the load-bearing defect", and
expect to read the findings rather than count them.

### Sol found a real defect that both expensive reviewers missed

On 67980, Sol flagged `sa_sfi_detect()` keeping `sfi_match_count`
across a non-consecutive name, where the pre-patch code reset it -- so
`1, 2, 100, 101, 200, 201, 202` trips the default `match_hit` of 4 with
a longest consecutive run of three, starting statahead for a workload
that does not meet the patch's own stated condition.

Verified by hand and still live in ps3 (renamed `sa_sfe_detect`), so it
was [posted to Gerrit](https://review.whamcloud.com/c/fs/lustre-release/+/67980/3/lustre/llite/statahead.c#2424).
Opus was picking apart the same function -- it filed defects at lines
2330 and 2393, five and fifty lines away -- and did not find it.

### Sol's precision was fine; its recall and its breadth were not

All four Sol findings were checked against the code by hand: **zero
false positives**. But three of the four were low-value (a layering
style note on a pattern that predates the patch, a "write a man3 page"
suggestion, a help-text ambiguity).

One systematic gap: Sol filed **zero commit-message findings** on any
of the four cases, where aireview filed 9 and opus 3, despite
review-core.md mandating that pass.

### The speedup is Sol doing less work, not working faster

2.0x faster, 2.5x fewer tokens, 2.5x fewer tool calls -- the three
ratios agree. That is a model doing a shallower investigation, not the
same investigation more efficiently. Worth remembering when a cheaper
model looks like a bargain on wall clock alone.

We could not confirm the hoped-for 9x cost saving: codex under a
ChatGPT plan reports no per-run dollar figure, so only the 2.5x token
ratio is measurable, and tokens are not comparable across providers as
a price proxy.

### Conclusion

Not a replacement for opus as the pre-landing gate -- 4 findings for 2x
the speed is a bad trade when the gate's job is recall.

The better argument for a cheap model is the disjointness: a second
pass is not redundant when the finding sets barely intersect. Sol
bought a live defect nobody else caught, in 7 minutes. That is a "run
it alongside" case, not a "run it instead" case.

### Worth testing next

- Higher effort on the same model -- does Sol's recall scale with
  effort, or is the breadth gap (commit messages especially) a
  property of the model?
- gpt-6-astra, to separate "cheap model" from "codex backend": if
  astra also files no commit-message findings, the gap is the backend
  or the prompt, not the model tier.
- The same model twice at the same settings, to measure run-to-run
  variance directly. Everything above is confounded by it.
