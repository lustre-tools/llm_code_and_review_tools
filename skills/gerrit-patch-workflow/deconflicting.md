# Deconflicting

What to do with the result of `gc conflicts`; `gc explain conflicts`
says what each field means. The tool models cherry-pick submit: two
changes collide only when they do not both land on the branch tip (or
-next), each with its in-flight parents, in either order; where either
sits now does not matter. Queued changes (in `<branch>-next`) are called
frozen here.

```bash
gc conflicts <cn>                        # summary first; --repo defaults to .
gc conflicts <cn> --hunks-for <other>    # conflict regions of one pair, repeatable
gc conflicts <cn> | jq -r '.status, ((.conflicts // [])[] | [.cn, .tier,
    .fix_side, (.via // "-"), (.own_via // "-"), .landing,
    (.files | join(","))] | @tsv)'       # one line per collision
```

The jq lines need jq and the default compact output, not `--full`.
`--human` prints the same result as a readable report grouped by the
change whose lines conflict: for showing a person, not for parsing.
Never start with `--hunks`: on a busy file it runs to megabytes. A hunk's
`line` counts the merged file with its conflict markers, not either side;
for a `truncated` hunk, find the place in both sides' files and read it.

## Reading the result

- `conflicts` (a landing order conflicts) and `silent_only` (lands
  cleanly but defines a test or value twice): the collisions to act on.
  Nothing else is a collision, whatever a merge onto another change's
  uploaded commit or a bot comment says.
- `via` / `own_via`: the lines are in that ancestor of the candidate /
  parent of the change, and `fix_side` is computed for it
  (`via_change`/`own_via_change` name the owner). Never change a parent
  you are not working on; report it to its owner.
- `on_next` `conflict`: the same collision seen against the queue (`with`
  names the frozen change). Ignore `stale`, `no-next` and `not-tried`.
- `on_branch`: `by` names the stack member that blocks the change;
  `conflict` with `under` and no `by` means the change itself conflicts
  on the tip, on top of its parent `under`.
- `watch`: build the combination and run the covering tests, first where
  your edit touches the shared function.
- `errors`: candidates that were not tried. Name them; they are not clean.

Give each landing and silent collision its own entry (the details are
under "Everything else" below). Summarise `watch` in one line: the
count, and by name at most about eight of them, only the frozen ones and
the release blockers (configurable, see `ranks`); if some come from your
own parents, count those apart by `own_via`.

## Own stack first

`status: needs-rebase` has no pairs: where the change collides is only
known once it lands. Rebase it locally as below, then rerun with
`--commit` (or `--worktree`) and judge the collisions on that result.

- `outdated_parent.status: clean`, or a clean `on_old_patchset` entry:
  it sits on an older patchset of its parent but lands on the tip with
  the current one. Under cherry-pick submit it lands as it
  is: leave it (a rebase would only cost votes); mention it in a line.
- `rebase_cause: parent`, or `outdated_parent.status: conflict`: the
  change (or `rebase_onto.for`) conflicts with its parent's current
  patchset and cannot land; its votes are lost either way. Rebase it
  locally onto `rebase_onto` (the parent's current patchset, unchanged),
  resolve it like any collision below, and rerun `gc conflicts --commit`
  on the result before judging the rest.
- `rebase_cause: branch` (`outdated_parent.status` is then `unknown`):
  if a rebase would move frozen or ready parents
  (`base.rebase`), wait for them to land and rebase once; otherwise a
  rebase onto the tip fixes it but drops votes, and the user decides.

## Who moves

`fix_side` is the default answer. It ranks frozen, release blocker,
release priority, ready, the rest; the higher one does not move, whoever
owns it. Never change the hash of a frozen change.

- `target`: the change being worked on moves. `candidate`: name the
  other owner (`owner`, `via_change.owner`) and what they would change;
  never edit their change. `coordinate`: propose an order.
- `mine` (top level, per pair, in `via_change`) says whether a change is
  the account's (null: no account configured). Believe a user who says
  the change is theirs or they are taking it over; if it is not `mine`
  and they have not said, report and ask. One owner of both: `fix_side`
  still says which moves.
- A user who is only reviewing gets a report, no edits.
- The target's owner may still move their change against `candidate` or
  `coordinate` when that is cheapest: offer it with its cost.

## Trivial collisions: fix them, then say so

Only when the user asked for the change to land cleanly, to fix what is
easy, or to deconflict it; that request covers the trivial fixes below
and nothing more. The summary never justifies an edit: rerun with `--hunks-for <cn>`
for exactly the collisions you would act on, landing conflicts with
`fix_side: target` first. Fix without asking if all of these hold:

- the change being worked on moves, and the lines are its own (no
  `own_via`); a pair whose `via` is another pair's candidate is fixed
  with that pair;
- every hunk of the pair, either order, is `insert-insert`,
  `adjacent` or `context`: one `edit-edit` makes the pair non-trivial;
- the fix only moves lines the change adds, or renumbers a value it adds
  (a fail_loc value, a test number: the define, its tests and the commit
  message) into a gap no open change uses. `git diff` shows your edit
  alone; judge the fix on `git diff HEAD~`, the change's own diff with
  the edit (in `git diff`, moving the change's block below an unchanged
  line looks like moving that line);
- behaviour does not change: no condition rewritten, nothing moved across
  ordering or locking, no hunk dropped, no new statement or flag;
- the diff stays about the size of the conflict, a few lines;
- Verify shows the fixed collisions gone and nothing new.

Finding the free value is part of the fix; the tool sees only the pairs
it tried. List the open changes on the file, read the values they add,
and take a gap: a free value at the shared append point still conflicts.
For a Lustre subtest number, `gerrit test-numbers <file> --suggest 3`
does this: it names gaps no open change touches.
Old changes (see "Old changes" in SKILL.md) do not hold a value.

```bash
gc search --all 'status:open project:<project> branch:<branch> file:<path>'
gc review <cn>          # each one's diff; or fetch its refs/changes ref
```

Tell the user in a line or two what moved and what it no longer collides
with, and that a renumbered value named in the commit message needs the
same edit on amend. If verification fails, undo the edit and report.

## Everything else: report first, edit on the user's choice

Deconflicting is one option, not the goal, and not at any cost. For a
collision that fails any test above, and for a rebase or restack other
than the parent rebase above, touch no file. Report per collision the
change, owner, `fix_side`, `via`/`own_via`, files and hunk kinds, then
the options with their cost: rearrange (how big); rearrange, but uglier;
defer the edit to whichever lands second; restack or rebase onto the
other (what it moves, which votes it drops; out when `base.rebase` would
move frozen or ready parents); wait for the other; leave it to its owner;
do nothing (it is far from landing). Recommend one. "This cannot be
deconflicted" is a complete answer. Fix the trivial ones and report the
rest in the same message. Only an explicit "go ahead and deconflict it"
also covers clean rearrangements beyond trivial (a larger move of your
own lines), never uglier ones. A review suggestion
that edits lines another change rewrites: raise it with the reviewer
(it will collide with that change) rather than silently declining it.

Techniques that worked: insert away from shared append points (mid-list,
mid-enum, mid-file), and never edit the last element's terminator
(`last:1;` to `last:1,`); keep an unchanged line between your edit and
theirs, and next to a large added block move the boundary line; put the
effect of a shared condition in a separate statement, then audit every
reader and writer of that state; drop your hunk when another change makes
the same edit, record the follow-up, and check tests that exist only on
the branch or -next; keep a contested line as is; defer cleanups in lines
others rewrite; redesign so it is not needed (owner's OK).

Verdict, one per collision: `rearranged` (the diff and the before/after
comparison; the one-line note for a trivial fix); `defer` (which landing
order is expected); `not possible` (why, and the alternative);
`needs uglifying`: the only rearrangement that removes it makes the code
worse. Do not apply that: say "this cannot be deconflicted without
uglifying the code", show the ugly variant, the clean alternatives and
the landing pressure (release hashtags, frozen). Only the user can weigh
that; if they accept, record the revert or follow-up where they keep
notes. Signs: code where no reader would look, flag-twiddling, logic only
there to dodge the conflict, a moved hunk that changes ordering or
locking, a diff bigger than the conflict; typically an `edit-edit`.

## Verify

Every edit, the trivial ones included. `--worktree` tests the uncommitted
edits of the change at HEAD (index and branches untouched); check a
mid-stack change out first (`git checkout <commit>`, `gc checkout <cn>`):

```bash
D=$(mktemp -d)
gc conflicts <cn> --commit HEAD --with <other> > "$D/before.json"
gc conflicts <cn> --worktree --with <other> > "$D/after.json"
for f in before after; do jq -r '((.conflicts // [])[] | "landing \(.cn) \(.landing)"),
    ((.silent_only // [])[] | "silent \(.cn)"),
    ((.watch // [])[] | "watch \(.cn)")' "$D/$f.json" > "$D/$f.txt"; done
diff "$D/before.txt" "$D/after.txt"
```

Pass `--with` for every change you deconflicted against (the filters may
drop one once it merges cleanly); an untracked file counts once added.
The fixed collisions must be gone and nothing new may appear. A fixed
pair still listed: `--hunks-for`
it in both runs; no remaining hunk may involve the change's own lines.
Then build, and run the covering tests on the change's base and, if
frozen, on -next, where the project allows (for Lustre a test VM, never
the development host). A pair in `watch` after the edit needs the
combination built too. Every edit here, a rebase included, stays local
and uncommitted until the user asks to commit or upload. If your project
keeps a change-log on uploads, say what was deconflicted.
