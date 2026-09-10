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
review?

**Two flaws in how this round was set up -- read the numbers with both
in mind.** First, it was run on the assumption that sol is a cheap
model traded off against opus. It is not: codex bills sol as the
"reliable agentic workhorse", an opus-class model. The budget tier is
gpt-5.6-luna ("fast and affordable") or gpt-5.3-codex-spark. So this is
closer to a peer comparison than a price/quality one, and sol's lower
finding count is not bought back by being cheap.

Second, the effort levels were not matched: sol ran at **medium**
against opus at **xhigh**, one rung apart on ladders that both go
higher. That handicapped sol independently of the tier question. Round
2 re-runs sol at high for this reason.

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

> **Round 2 substantially walked this back -- see below.** With runs at
> matched effort, gpt-6-astra and opus agree on roughly 60% of each
> other's findings, hand-verified. The disjointness measured here was
> inflated by comparing the weakest setting in the set (sol at medium,
> 1-2 findings per case) against aireview's 5-7, where a low
> intersection is close to guaranteed by sample size, and aireview is a
> different tool at a different date besides. The observation below is
> kept as written because the caution it draws is still right.

Across 36 findings from three reviewers on byte-identical code there
was **essentially one partial overlap** -- opus at `statahead.c:2330`
and aireview at `:2352`, both about `reset:` doing `*pid = 0`, arguing
different consequences.

Opus overlaps aireview about as poorly as Sol does. So Sol's 4-vs-22 is
not "5x worse recall against a fixed truth". Local history points the
same way: the LU-19895 DIO patch reviewed five times by opus produced
6, 5, 4, 6 and 3 findings.

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

Not a replacement for opus as the pre-landing gate on this evidence --
4 findings against 10 is a wide gap when the gate's job is recall.
But the gap is measured at an unfair effort setting and against a model
that is a peer rather than a bargain, so treat it as provisional until
round 2.

The durable result is the disjointness: a second pass is not redundant
when the finding sets barely intersect, and sol found a live defect
nobody else caught. That argues for stacking reviewers, but the
argument needs a genuinely cheap model to be attractive -- stacking two
opus-class reviewers is a different budget conversation.

### Worth testing next

- Higher effort on the same model -- does sol's recall scale with
  effort, or is the breadth gap (commit messages especially) a
  property of the model? (round 2)
- gpt-6-astra at high, flagship against flagship, to separate model
  tier from codex backend: if astra also files no commit-message
  findings, the gap is the backend or the prompt, not the tier.
  (round 2)
- **An actually cheap model** -- gpt-5.6-luna or gpt-5.3-codex-spark --
  which is what the "cheap second opinion" idea needs and what round 1
  did not test.
- The same model twice at the same settings, to measure run-to-run
  variance directly. Everything above is confounded by it.
- opus at max, or sol at ultra, to see whether either ladder is even
  saturated at the settings used here.

---

## Round 2 -- 2026-09-09: effort matched at high, across three codex models

**Question.** Round 1 compared sol at medium against opus at xhigh,
which was not a fair fight. Re-run the codex models at **high** and
compare against the existing opus xhigh numbers. Also test an actually
cheap model, which round 1 never did.

**Setup.** Same four cases, same SHAs, `--jobs 4`, full mode. Three new
runs -- gpt-5.6-sol high, gpt-6-astra high, gpt-5.6-luna high -- against
round 1's opus xhigh. No new opus run.

| run | findings | cases | mean wall | tokens | tool calls |
|---|---|---|---|---|---|
| gpt-6-astra high | 8 | 3 (1 refused) | 9m06s | 7.5M | 153 |
| opus xhigh | 10 | 4 | 16m06s | 23.8M | 243 |
| gpt-5.6-sol high | 6 | 4 | 14m30s | 19.2M | 172 |
| gpt-5.6-sol medium | 4 | 4 | 8m06s | 9.6M | 121 |
| gpt-5.6-luna high | 3 | 3 (1 failed) | 11m20s | 21.7M | 272 |

### gpt-6-astra is the result worth acting on

Astra found 2.7 findings per case against opus's 2.5, on **one third of
the tokens** and 56% of the wall clock. It is the best findings-per-
token in the set by a wide margin: 0.94M tokens per finding, against
opus 2.4M, sol high 3.2M and luna 7.2M.

Agreement supports the quality, not just the count. Astra and opus are
the strongest-agreeing pair in the matrix -- astra->opus 5 of 8,
opus->astra 6 of 10 -- and the matches survive hand-reading:

- `/COMMIT_MSG` on 67980: both propose the *same* corrected trailer,
  `Fixes: 629b0534e141 ("LU-14361 statahead: add support for mdtest
  shared dir workload")`.
- `statahead.c:2330`: both describe `reset:` clearing a slot before the
  ownership check, both reaching for a `README` stat by a
  pid-colliding task as the example.
- `lfs-setstripe.1:329`: astra, opus **and** sol high independently say
  the "a decimal unit is refused here as well" wording overstates the
  code, astra and opus using the same `1024MB = 15625 * 65536` example.

Two flagships converging on the same line with the same reasoning is a
much better signal than either finding it alone.

### Raising effort is not a recall dial

Sol at high cost roughly double sol at medium -- 8m06s to 14m30s, 9.6M
to 19.2M tokens -- to go from 4 findings to 6. Worse, it **lost the
one finding that mattered**: sol medium's `statahead.c:2347` match-count
regression was gone, replaced by two identical `(style)` notes about a
redundant NULL guard before `OBD_FREE_PTR_ARRAY()`.

More effort produced more output and less value. Do not assume a weak
result at medium becomes a good result at high.

### The cheap tier is not cheap

Luna is billed "fast and affordable" and was the natural candidate for
the cheap-second-opinion idea round 1 wanted to test. It was the worst
run in the set on every axis that matters: 3 findings, 21.7M tokens
(more than opus), 272 tool calls (more than opus), and one case that
failed outright.

There is no cheap-second-opinion story here. Astra is both cheaper in
tokens and better.

### Two operational failure modes, both caught as `failed`

Worth knowing before pointing automation at these backends. In both
cases lreview recorded `failed` rather than silently reporting the
patch clean, which is the metadata-as-completion-marker design earning
its keep.

1. **astra refused a security patch.** On 68468 (`ptlrpc: do not unpack
   req in gssiam_extract_h_exp`, a GSS wire-parsing change) the
   provider returned *"This content was flagged for possible
   cybersecurity risk"* and the turn failed. Sol reviewed the same
   patch fine at medium and high, so this is model-specific, not a
   codex-backend limit. For Lustre this is disqualifying for astra as
   the *only* gate -- GSS, sec and lnet code is in scope, and a
   reviewer that refuses part of the tree cannot be the last word.
2. **luna ignored the output contract.** On 68339 luna ran to
   completion -- clean `turn.completed`, 6.9M tokens -- and wrote
   neither `gerrit-review.json` nor `review-metadata.json`, delivering
   its review as a chat message instead. A backend that sometimes does
   not produce its artifact cannot be automated.

### Recommendation

- **gpt-6-astra at high** is a credible opus alternative on cost and
  the best codex option tested: comparable per-case yield, a third of
  the tokens, and high agreement with opus on what it does find.
  Caveat: it cannot be the sole reviewer while it refuses security
  patches.
- **opus xhigh** stays the gate, on breadth and because it reviews
  everything.
- **sol** at either effort, and **luna**, are not recommended for this
  work on this evidence.
- Stacking astra with opus is now the interesting configuration: they
  agree enough to corroborate each other and differ enough that astra
  contributed a novel `statahead.c:2456` defect and a novel
  `vvp_io.c:1579` race that opus described differently.

### Still not measured

- **Run-to-run variance at fixed settings.** Everything above is
  confounded by it, and sol medium-vs-high hints it is large. This is
  now the most valuable next experiment: the same model, same effort,
  twice.
- Whether astra's GSS refusal is deterministic -- it was seen once, and
  was not retried.
- Four cases, all 25-431 lines, all changes that already drew an AI
  review. Small and skewed.
