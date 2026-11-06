# Backport review

Your first task is to prove that this commit is an honest and correct
translation of the upstream change to this branch. This task adds to the
protocol. It does not replace any part of it: code that matches upstream gets
the same regression analysis as any other change. A defect that the commit
copies from upstream is not a defect of the backport, but you still report it.
Classify every finding.

## Checks

- Every upstream hunk is present, or the commit message explains its absence.
- Every difference from upstream is needed on this branch and is correct.
- The upstream change does not depend on a commit that this branch lacks.
- Code that exists only on this branch gets the same change where it needs it.
- Trailers, `Fixes:` targets and test version gates suit this branch.
- For a `Lustre-commit: TBD` trailer, the port matches the current upstream
  patch set, and a landed upstream change has its landed hash in the trailer.

## Classes

Start each comment with its severity marker and then its class, for example
`(defect) BACKPORT: ...`.

- `BACKPORT`: the port introduces the problem, or the problem exists only on
  this branch. This class includes an upstream defect that is harmless on
  master but reachable here. Set `unresolved: true`.
- `UPSTREAM`: the same defect exists on master, and it does not block this
  port. Say that the fix goes to master first and then gets its own port.
  Set `unresolved: false`. This finding does not justify a refresh of the
  port, even with the `(defect)` marker.
- `UPSTREAM-BLOCKER`: the same defect exists on master, the port adds it to
  this branch, and it is too severe to ship. Only these defects qualify: data
  loss or corruption, a reachable crash, LBUG or hang, a security fault, and
  the failure of a common operation. Set `unresolved: true`. Say "Do not land
  until fixed upstream." in the top-level `message`.

Every other inherited defect is `UPSTREAM`. This rule includes a performance
cost, log noise, a rare leak and a wrong error code.

## Rules

- Before you report `UPSTREAM` or `UPSTREAM-BLOCKER`, read the upstream code
  and confirm that it has the same defect.
- If you cannot read the upstream commit, say so in the top-level `message`.
  Do not guess a class: report the finding as `BACKPORT`.
- Report a `(minor)` item in code that matches upstream as `UPSTREAM`. Do not
  report `(style)`, `(typo)`, `(suggestion)` or `(nit)` items in that code.
- The verdict in the top-level `message` depends only on `BACKPORT` and
  `UPSTREAM-BLOCKER` findings.
- In `review-metadata.json`, count and score only `BACKPORT` and
  `UPSTREAM-BLOCKER` findings.
