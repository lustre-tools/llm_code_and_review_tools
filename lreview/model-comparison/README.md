# AI review comparison data

A fixed set of Lustre patchsets, the review findings each AI backend
produced on them, and the metrics needed to compare backends: wall
clock, tokens, cost, and how many tool calls the agent actually made.

The point is to answer "is model X good enough to review Lustre
patches, and what does it cost?" without re-litigating the setup every
time a model ships. Add a run, re-run `compare.py`.

**[OBSERVATIONS.md](OBSERVATIONS.md) is the running log of what these
runs have actually shown** -- read that first; this file is just how to
use the data.

## What is here

```
OBSERVATIONS.md            what we have learned so far, per round
cases/manifest.json        the patchsets under test, pinned by revision SHA
ground-truth/aireview.json what the Gerrit 'aireview' bot said on each one
runs/<run-id>/run.json     one backend's results: metrics + findings
runs/<run-id>/findings/    the raw gerrit-review-*.json per case
runs/<run-id>/reports/     the human-readable Markdown reports
runs/<run-id>/logs/        the agent event log per case, gzipped
import_run.py              lreview results dir -> runs/<run-id>/
compare.py                 the comparison tables
```

Every case is pinned to **one patchset's revision SHA**, so each run
reviews byte-identical code no matter how the change moves afterwards.
That is the whole reason the numbers are comparable; do not "refresh" a
case to a newer patchset -- add a new case instead.

## Reproducing a run

```bash
lreview run --repo ~/lustre-release --agent codex --model sol \
    --effort medium --jobs 4 --results-dir /tmp/bench-sol \
    $(python3 -c "import json;print(' '.join(c['lreview_arg'] for c in json.load(open('cases/manifest.json'))['cases']))")

./import_run.py /tmp/bench-sol 2026-09-09-gpt-5.6-sol-medium \
    --note "codex gpt-5.6-sol, --effort medium, jobs 4"
./compare.py
```

Keep `--jobs` the same across runs being compared. Reviews are
API-bound rather than CPU-bound, but the parallelism still shapes how
often a run meets a provider rate limit, which shows up as wall clock.

Run ids are `<date>-<model>-<effort>`.

## Reading the comparison

`compare.py` prints per-run totals, then per-case finding counts with a
crude overlap measure against the aireview reference (same file, and
same file within 25 lines).

**The overlap numbers are a pointer, not a score.** Two reviewers can
describe the same defect at different lines, or two different defects
five lines apart -- string and line matching cannot tell those apart.
Read the findings before drawing a conclusion; `compare.py --findings`
prints them.

## What the reference set is and is not

`ground-truth/aireview.json` is what the Gerrit `aireview` bot posted
on that patchset, with the author's replies. It is a useful reference
because it is a review from a more expensive model that real reviewers
and patch authors already acted on.

It is **not** ground truth. It is not complete -- the 2026-09-09 runs
found real defects it missed, including a
[statahead regression](https://review.whamcloud.com/c/fs/lustre-release/+/67980/3/lustre/llite/statahead.c#2424)
that opus xhigh also missed -- and it is not guaranteed correct. A
finding that does not match the reference is not thereby wrong; it has
to be checked against the code, by hand.

The first round of runs found near-zero overlap between *any* two
reviewers, including between two expensive ones. Low reproducibility is
the headline result so far, so beware of reading a single run as a
model's true score.

## Caveats worth keeping in mind

- **Four cases is a small sample.** Finding counts in the single digits
  swing easily; a one-finding difference between runs is noise.
- **Cost is not always available.** claude reports a dollar figure per
  run; codex under a ChatGPT plan does not, so those rows show `-` and
  only tokens can be compared.
- **Token counts are not comparable across providers** as a proxy for
  price -- different tokenizers, different per-token rates, different
  cache accounting. Compare tokens within a provider, cost across them.
- **The cases skew small-to-medium** (25 to 431 changed lines) and
  toward changes that already attracted an AI review.
