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
- The `Change-Id:` is the same as the upstream Change-Id. A wrong Change-Id
  needs a new Gerrit change, because Gerrit cannot change the Change-Id of an
  existing change. Say so in the comment.
- Each `Lustre-change:` names the right upstream change: its subject and
  Change-Id match the port.
- The port is not stale. It matches the landed commit, or the current patch
  set of an upstream change that has not landed. A port of an older patch set
  is a `BACKPORT` defect.
- A landed upstream change has its landed hash in `Lustre-commit:`, not TBD.

## Squashed ports

A port can squash an upstream change with its upstream follow-up fixes. Then:
- Each squashed change has its own `Lustre-change:` and `Lustre-commit:` pair.
- The Change-Id of each squashed change becomes a `Was-Change-Id:` line.
- Only the Change-Id of the final change stays as `Change-Id:`.
- The port must not squash unrelated changes.

## Unresolved upstream review comments

The facts above can list unresolved AI review comments on an upstream change
that has not landed. A port can land before its upstream change, so these
comments can be the only record of a known defect. Check each comment against
the port:
- It is a real `(defect)`, and it applies to the port: report it as
  `UPSTREAM-BLOCKER`, and link the upstream comment.
- It is real but not a `(defect)`, and it applies to the port: report it as
  `UPSTREAM`, and link the upstream comment.
- It does not apply because the port code is different: report an
  `UPSTREAM` note with `unresolved: false`. Say why it does not apply, and
  link the upstream comment.
- It is wrong: say so in one sentence in the top-level `message`.

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
  the failure of a common operation. An unresolved upstream review comment
  can also make a finding `UPSTREAM-BLOCKER`; see the section above. Set
  `unresolved: true`. Say "Do not land until fixed upstream." in the top-level
  `message`.

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
