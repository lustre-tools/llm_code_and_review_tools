---
name: gerrit-patch-workflow
description: This skill should be used for work on Gerrit changes with the gerrit/gc CLI - "what comments are on this patch", "reply to the review feedback", "address the reviewer comments", "mark that thread done", "shepherd this patch series", "what is the status of my patches", "post a review on this change", "add a reviewer", "check the CI status of my patches", "upload the new patchset", "push this commit to Gerrit", "push this as a private patch", "upload a private change", "rebase and repush the series", "is there already a patch for this", "find the follow-up to this change", "does this conflict with", "deconflict this patch", "will this land cleanly", "can we avoid the rebase". Covers comment triage, staged replies, uploading patchsets, multi-patch series sessions, finding related changes, deconflicting against other open changes, and Lustre commit-message rules.
version: 0.1.0
---

# Gerrit patch workflow

`gerrit` (aliases `gc`, `gerrit-cli`) covers review comments, patch series
sessions, change metadata and CI overview. Output is JSON; add
`--envelope` for the full wrapper, skip `--pretty`. `gc examples` prints
the built-in workflow cheatsheets, `gc explain <command>` details any
command, and `gc describe` emits the machine-readable API for all of them.

A change is addressed by full URL, bare number or Change-Id -- the
Change-Id being the handle a checkout has, from the commit message. Most
commands remember the URL from the last `gc comments`, so later calls can
omit it.

```bash
gc info 64086
gc info If2706506135264f501c6cbc6243ed449f9792605
gc info $(git log -1 --format=%B | sed -n 's/^Change-Id: //p')
```

A Change-Id shared across branches (a backport) resolves to the open
change; if more than one is open, the error lists them and you pass the
number.

## Reading feedback

```bash
gc comments <url>                 # unresolved threads, with code context
gc comments 64086 --all           # include resolved
gc series-comments <url>          # every patch in the series at once
gc comments 64086 --fields=index,id,file,message
gc comments 64086 --all --timeline --since '2026-10-01 17:00'
                                  # who said what when, oldest first, replies too:
                                  # "was my comment answered?"
gc comments 64086 --messages --include-ci
                                  # the change messages alone, oldest first: top-level
                                  # posts like a bare "BUILD", and the CI answers
gc comments 64086 --all --include-system
                                  # every change-level message, each vote with its
                                  # reviewer and patchset, uploads and CI too
gc comments 64086 --all --include-ci --since '2026-10-01 17:00'
                                  # what happened since then: threads with a comment
                                  # at or after it (same index) and newer messages
gc comments 64086 --all --timeline --since 2h
                                  # --since also takes an age: 90m, 2h, 1d, 1w (UTC)
```

Read **all** the feedback, human and bot. Bots -- `aireview` in particular
-- post substantive findings, often on `/COMMIT_MSG` as well as on code,
and they re-run per patchset. Triage each one like a human comment.

The tools report what their default filters hid and whether threads are
still open in a `hint` field. Read it rather than assuming the first
listing is everything.

## Replying

```bash
gc reply 0 --done                 # 'Done' + resolve, on the remembered URL
gc reply 2 --ack
gc reply 8468c1b7_1b15e7ef "Fixed in PS4"   # by the thread's root_comment.id
gc done <url> <idx>               # same as reply --done
gc ack <url> <idx>
```

An index is looked up afresh each time in the unresolved listing, so once
a reply resolves thread 0, every later index shifts down by one and a
second `gc reply 1` lands on what `gc comments` showed as 2. For more than
one reply, address threads by `root_comment.id` (each thread's `index` and
`root_comment.id` are in `gc comments`; `--fields=index,id` gives both, and
each `--timeline` entry has its comment `id`). To answer several threads,
prefer one `gc batch` with `comment_id` entries (below) over a run of
`gc reply` calls.

For several replies at once, stage them, look at them, then push:

```bash
gc stage --done 0
gc stage 1 "Will fix in a follow-up"
gc staged list
gc push --dry-run <change-id>
gc push <change-id>
```

`gc push` posts staged **comment replies**. It does not push commits;
`gc upload` does -- see "Uploading a patchset" below. However many
patchsets the threads are on, the replies are published as one
message. Push refuses while the account has other drafts on the change
(publishing would take them along); say so rather than deleting them.

Or write the replies to a file and post them together -- one review, one
mail to everyone on the change, instead of one per reply:

```bash
gc batch <change> replies.json    # '-' reads the JSON from stdin
```

```json
[{"comment_id": "8468c1b7_1b15e7ef", "message": "Done in PS8", "mark_resolved": true},
 {"comment_id": "9005efd6_909ba5e1", "message": "Kept: the osc path still needs it"}]
```

`comment_id` is the exact address and reaches resolved and bot threads
too; entries can instead use `thread_index`, or `file` and `line`.
In the output of `reply`, `done`, `ack` and `batch`, `comment_id` is the
comment replied to and `reply_id` the id of the reply just posted.

Declining a finding is a legitimate outcome -- reply with why. Do not
silently skip a comment, and do not answer a finding by adding an
explanatory code comment (see the lreview spin-cycle skill for why that
habit wrecks a patch).

Gerrit renders Markdown in replies, `gc message` text and review messages,
so a plain-text table there loses its alignment. Put any table, column-aligned
output or log excerpt in a fenced block (three backticks on their own line
before and after) or indent every line 4 spaces:

````
```
test       before   after
sanity 17h FAIL     PASS
```
````

## Posting a whole review from JSON

```bash
gc review --post-comments findings.json <url> --prefix '[AI review]' --dry-run
```

The file carries `message`, `vote`, `tag` and `comments` (Gerrit REST dict
or a flat list): one call posts the cover message and every inline
comment. `--dry-run` prints the exact payload without posting. Details in
`gerrit_cli/README.md`, section "Posting a Review from JSON".

## Series sessions

For a stack of dependent patches, use a session rather than juggling URLs:

```bash
gc review-series <url>    # start; shows patches and the review prompt
gc status                 # where the session is
gc work-on-patch <url>    # checkout one patch and show its comments
gc finish-patch           # finish current patch, rebase, move on
gc next-patch
gc abort                  # end session, discarding changes by default
```

`gc continue-reintegration` and `gc skip-reintegration` drive a rebase
that hit conflicts.

## Status and CI

```bash
gc info <url>             # patchsets, reviews, CI at a glance
gc series-info <url>
gc series-status <url>    # whole series, one table
gc related <url>
gc review <url>           # the change itself, 3 lines of context (-u N)
gc diff <url> [A [B]]     # what changed between patchsets; default the
                          # newest against the one before it
gc maloo <url>            # enforced/optional test summary
gc watch patches.json     # Maloo triage across a watched list
```

`gc watch` takes a JSON array of objects with a `gerrit_url` field. For
what to do with a failure, use the CI triage skill.

## Finding related changes

"Is there already a patch for this", "where did the follow-up go":
`gc search` passes Gerrit's query syntax through, so narrow the query on
the server rather than filtering a small page yourself. Roughly from the
strongest link to the weakest:

```bash
gc related <url>                                      # the relation chain
gc search 'message:"LU-20566" -is:abandoned'          # a ticket
gc search 'message:"783c13bb0b8f" -change:62757'      # Fixes: citations
gc search --all 'project:fs/lustre-release branch:master
    path:"lustre/tests/sanity-ec.sh" after:2026-08-22 -is:abandoned'
gc search 'project:fs/lustre-release hashtag:pt_ecro after:2026-08-22'
```

- The tickets worth searching are not only the change's own: a patch
  often parks work under another one in its own code --
  `always_except LU-20566 41j 41k`, `/* LU-19999: ... */` -- and a reply
  names where it goes ("filed LU-20566"). The own ticket of a series
  matches every sibling.
- `after:` is "modified after", not "created after"; Gerrit has no
  creation filter. An open follow-up that nobody touched since the
  promise was made is still a follow-up, so do not throw out what was
  created earlier.
- A file search is only as good as the file: a comment on the commit
  message or a change-level comment has none, so take the file from what
  the thread is about -- the test list, the function -- and grep the
  candidates' diffs for it rather than trusting that a change touching
  the file does the work.
- Results come newest-updated first and one page by default;
  `more_results` in the output means there are more, and `--all` follows
  them.
- On review.whamcloud.com `tr:`/`bug:` return nothing (no tracking-id
  config) and `footer:"Fixes=..."` misses Lustre's `Fixes: sha ("...")`
  trailer -- use `message:` for both. `comment:<number>` matches far too
  much to be useful there.

## Deconflicting

"Does this conflict with", "will this land cleanly", "can we avoid the
rebase": `gc conflicts` trial-merges one change against the open changes on
its branch that are likely to land soon, in both landing orders and as
uploaded. `gc explain conflicts` defines every field; this section is
about what to do with them.

```bash
gc conflicts <cn> --repo /abs/path/to/checkout                     # summary first
gc conflicts <cn> --repo /abs/path/to/checkout --hunks-for <other>  # repeatable
gc conflicts <cn> --repo /abs/path/to/checkout --commit <sha>       # a local edit
gc conflicts <cn> --with <other> --all    # one given change; the 'other' tier too
gc conflicts <cn> --repo . | jq -r '.status, ([(.conflicts // [])[] | .b = "landing"]
    + [.as_uploaded_only[] | .b = "uploaded"] | .[] | [.b, .cn, .tier, .fix_side,
    (.via // "-"), (.own_via // "-"), .landing, .as_uploaded,
    (.files | join(","))] | @tsv)'                    # one line per collision
```

`--full` adds each pair's `commit` and `ps` and the lists behind the
counts. Never start with `--hunks`: on a busy file it runs to megabytes.

**When.** Before uploading to a change in a busy area; when Gerrit or a
bot reports a merge, cherry-pick or -next conflict; after a neighbour
lands or is picked; before accepting a review suggestion that edits lines
another in-flight change rewrites (decline or defer it, and say why).

**Reading the result**, heaviest first:

- `conflicts`: a landing order conflicts, what happens when both land.
- `as_uploaded_only`: landing is clean, only the uploaded commits collide
  (what the other owner, or CI on a stale base, sees). Act on it only
  when the user is about to rebase or the other change lands next. With
  `status: needs-rebase` it is the only model and `conflicts` is null.
- `via`: the colliding lines are in that ancestor of the candidate, so
  that change is the one in the way; `fix_side` already reflects it.
- `own_via`: the colliding lines are in that parent of the change, not
  in the change. Fix them there only if the user is working on that
  parent; never change a parent you are not working on.
- `on_next`: the change on `<branch>-next`; a `conflict` names the queued
  change in `with`. The same pair may also be in `conflicts`: one
  collision, seen against the queue. Ignore `stale` and `no-next`.
- `on_branch`: the change's own stack on the branch tip; `under` names
  the stack member below it, `by` the one that blocks it.
- `silent_only`: a clean merge that still defines a test, fail_loc or
  OBD_CONNECT value twice, or adds the same test to ALWAYS_EXCEPT twice:
  a collision like any other.
- `watch`: a clean merge, but both edit the functions in `same_function`:
  build the combination and run the covering tests, first the pairs whose
  shared function your edit touches, then by tier.
- `base_drift` (the branch moved between the two bases),
  `already_conflicting` (conflicts without your change) and `skipped`
  (needs a new patchset anyway): not collisions to plan around.

Report the landing conflicts one line each; `as_uploaded_only` and
`watch` one line each, with counts, naming only the frozen ones and those
with a release blocker or priority hashtag (configurable; `ranks` shows
the globs in effect).

**Own stack first**: it changes the base the rest is measured on.

- `outdated_parent.status: clean`, or `on_old_patchset` without a
  `parent` conflict: it sits on an older patchset of its parent but
  applies on the current one. Do not rebase (a project that submits by
  cherry-pick lands it anyway); mention it in one line.
- `rebase_cause: parent`, or `outdated_parent.status: conflict`: the
  change (or `rebase_onto.for`, below it) conflicts with its parent's
  current patchset and cannot land as it is, so its votes are lost
  either way. Rebase it locally onto `rebase_onto`, the parent's commit
  byte-identical to its current patchset on Gerrit, and resolve it like
  any collision below (files only, no hunk kinds: read it in the rebase).
  Rerun `gc conflicts --commit` on the result before judging the rest.
- `rebase_cause: branch` and `base.rebase` says a rebase would move
  frozen or ready parents: wait for them to land, then rebase once. Any
  other rebase drops votes and needs the user's OK.

**Who moves.** `fix_side` is the default answer. It encodes who goes
first: frozen (in -next), release blocker, release priority, ready, the
rest. The higher one does not move, whoever owns it. Never change a
picked change's hash to make room.

- `target`: the change being worked on moves.
- `candidate`: the other owner's move. Name them and say what they would
  change; never edit their change.
- `coordinate`: propose an order and who goes first.
- `owner` against `me` is Gerrit's record: if the user says the change
  is theirs or they are taking it over, believe them. Moving their own
  change against `fix_side`, when cheapest, is an option to offer with
  its cost, never your silent choice.
- A user who is only reviewing, or did not ask for a change, gets a
  report only.

**Trivial collisions: fix them, then say so.** The summary never
justifies an edit: rerun with `--hunks-for <cn>` for exactly the
collisions you would act on, landing conflicts with `fix_side: target`
first. When the user is working on the change and wants it to land
cleanly, fix a collision without asking if all of these hold:

- the change being worked on moves, and the lines are its own (no
  `own_via`);
- every hunk of the pair, any model and order, is `insert-insert`,
  `adjacent` or `context`: one `edit-edit` makes the pair non-trivial;
- the fix only moves or renumbers lines the change adds, judged on its
  own diff against its parent (`git diff "$P" "$C"`, see Verify); in the
  working-tree diff, moving the change's block below an unchanged line
  looks like moving that line;
- behaviour does not change: no condition rewritten, nothing moved across
  ordering or locking, no hunk dropped, no new statement or flag;
- the diff stays about the size of the conflict, a few lines;
- Verify shows the fixed collisions gone and nothing new.

A conflict on a value space (fail_loc value, test number) needs a value
no open change uses, and the tool only sees the pairs it tried. Grep the
diffs of the open changes on that file before picking one, and take it
from a gap: a free value at the shared append point still conflicts.

```bash
gc search --all 'status:open project:<project> branch:<branch> file:<path>'
```

Tell the user in a line or two what moved and what it no longer collides
with. If the fix changes something the commit message names (a fail_loc
value, a test number), say the message needs the same edit when the
change is amended; an uncommitted edit cannot carry it. If verification
fails, undo the edit and report.

**Everything else: report first, edit on the user's choice.**
Deconflicting is one option, not the goal, and not at any cost. For a
collision that fails any test above, and for a rebase or restack other
than the parent rebase above, touch no file. Report per collision the
change, its owner, `fix_side`, `via`/`own_via`, files and hunk kinds,
then the options with their cost: rearrange (how big); rearrange, but
uglier; defer the edit to whichever lands second; restack or rebase onto
the other (what it moves, which votes it drops; out when `base.rebase`
would move frozen or ready parents); wait for the other to land; leave it
to the other owner; do nothing (the other is far from landing).
Recommend one. "This cannot be deconflicted" is a complete answer. A
mixed result is normal: fix the trivial ones and report the rest in the
same message. "Go ahead and deconflict it" also covers clean
rearrangements beyond trivial, never uglier ones.

**Techniques** (patterns from real cases):

- Insert away from shared append points (mid-list, mid-enum, mid-file);
  never edit the last element's terminator (`last:1;` to `last:1,`).
- Keep an unchanged line, a blank one will do, between your edit and
  theirs; next to a large added block, move the boundary line, not the
  block.
- Don't rewrite a shared condition; put the effect in a separate
  statement, then audit every reader and writer of the state it changes.
- Leave gaps when adding several numbers; move a new test to a gap or
  another script, helpers away from where others append.
- Drop your hunk when another change makes the same edit, record the
  follow-up, and check tests only on the branch or -next for breakage.
- Keep a contested line as is, style nit included, and say why; defer
  cleanups in lines others rewrite.
- Redesign so the contested code is not needed (needs the owner's OK).
  Code that anticipates another change can look wrong until it lands:
  say so in the commit message or change-log.

**Verdict, one per collision:** `rearranged` (the diff and the
before/after comparison; for a trivial fix the one-line note); `defer`
(which landing order is expected); `not possible` (why, e.g. the same
lines must change both ways or frozen parents block the rebase, and the
alternative); `needs uglifying`: the only rearrangement that removes it
makes the code worse. Do not apply that: say "this cannot be deconflicted
without uglifying the code", show the ugly variant, the clean
alternatives and the landing pressure you see (release hashtags, -next).
Only the user can weigh that; if they accept, record the revert or
follow-up where they keep notes. Signs: code where no reader would look,
intent hidden by flag-twiddling, logic that exists only to dodge the
conflict, a moved hunk that changes ordering or locking, a diff bigger
than the conflict, a reviewer asking "why is this here?". An `edit-edit`
on one condition or declaration is the usual case.

**Verify** every edit, the trivial ones included. Edit with the change's
commit checked out: your local commit, also mid-stack, or the uploaded
one (`gc checkout <cn>`, or `git fetch <remote>
refs/changes/<last 2 digits>/<cn>/<ps>` and `git checkout FETCH_HEAD`).
Compare a run on that commit with one on the edit, built as a throwaway
commit on the change's parent in a temporary index (the user's index
stays as it is):

```bash
X=<change-commit>; P=$(git rev-parse "$X^"); D=$(mktemp -d)
gc conflicts <cn> --repo . --commit "$X" --with <other> > "$D/before.json"
GIT_INDEX_FILE=$D/index git read-tree "$X" && GIT_INDEX_FILE=$D/index git add -u &&
  C=$(git log -1 --format=%B "$X" |
      GIT_INDEX_FILE=$D/index git commit-tree "$(GIT_INDEX_FILE=$D/index git write-tree)" -p "$P" -F -)
git diff "$P" "$C"                     # the change's own diff, as judged above
gc conflicts <cn> --repo . --commit "$C" --with <other> > "$D/after.json"
for f in before after; do jq -r '((.conflicts // [])[] | "landing \(.cn) \(.landing) \(.as_uploaded)"),
    (.as_uploaded_only[] | "uploaded \(.cn) \(.as_uploaded)"),
    (.silent_only[] | "silent \(.cn)"), (.watch[] | "watch \(.cn)")' "$D/$f.json" > "$D/$f.txt"; done
diff "$D/before.txt" "$D/after.txt"
```

`add -u` takes tracked files; add a new file by name. Pass `--with` for
every change you deconflicted against (the filters may drop one once it
merges cleanly), with the same flags in both runs. The collisions you
fixed must be gone and nothing may appear that was not there before;
unrelated as-uploaded entries may stay. When a fixed pair is still
listed, `--hunks-for` it in both runs: no remaining hunk may involve the
change's own lines.

Then build and run the covering tests on the change's base and, if
queued, on -next. A pair in `watch` after the edit, or one sharing a
function, signature or struct, needs the combination built too: a clean
merge can still fail to build. Every edit here, a rebase included,
stays local and uncommitted until the user asks to commit or upload;
then a content change gets a short change-log ("Deconflicted with #A,
#B: ...; no functional change").

## Deleting comments and change messages

Published comments cannot be edited -- Gerrit has no such operation. A
correction is a reply in the thread. Deleting is for an account with
Gerrit's Administrate Server capability, and only when the user asked
for that comment or message to go:

```bash
gc comments <url> --all                 # comment ids: root_comment.id, replies[].id
gc delete-comment <url> <comment_id> --reason "..." --dry-run
gc comments <url> --messages            # change-message ids
gc delete-message <url> <id-prefix> --reason "..." --dry-run
```

- Run `--dry-run` first and check it names the text you mean to delete.
  There is no undo: Gerrit keeps the entry but replaces its text with
  "Comment removed by: <name>; Reason: <reason>" (a comment is also
  resolved). The reason is public; it follows the same prefix rule as
  anything else posted from the account.
- Every deletion rewrites the change's history: the deleted message and
  every later change message get new ids. List `--messages` again
  before each `delete-message`; never reuse an id from before a
  deletion. Comment ids do not change.
- Both are refused in drafts mode.

## Change metadata

```bash
gc vote <url> <label> <score>        gc message <url> "text"
gc set-topic <url> <topic>           gc hashtag <url> --add <tag>
gc rebase <url>                      gc checkout <url>
gc abandon <url>                     gc restore <url>
gc reviewers <url>                   gc add-reviewer <url> <name> [<name> ...]
gc find-user <name> [<name> ...]     gc remove-reviewer <url> <name>
gc reviewer-candidates <url> --repo <tree> --ref upstream/master
```

`add-reviewer` and `find-user` do fuzzy name matching -- confirm the match
before adding someone to a change. `find-user` marks bot accounts `is_bot`.
Look several people up in one `find-user` call and add several in one
`add-reviewer` call (no shell loops). `add-reviewer` matches every name
before adding anyone, and each entry of its `added` is Gerrit's own answer
(`added_reviewer`, `added_cc`, `already_on_change` or `error`), so running
`reviewers` afterwards to check is not needed.

`reviewers` lists everyone on the change, CCs included: tell them apart by
`role` (`REVIEWER` or `CC`), and skip `is_bot` accounts (`service_user`
misses some bots, aireview among them), the `owner` and the
`commit_author` (the commit's author, who may be only a CC when a bot
owns the change). `human_participant_count` is the number of people
already on the change, and `people_besides_owner` the same without the
owner: the count to check a cap on people against. Per person it gives `added_at` (when they took the
role they hold now), `votes` standing on the current patchset,
`copied_votes` (the ones Gerrit carried forward from an earlier
patchset), `voted_on_current_patchset`, `last_vote`, and `last_activity`:
their latest message or inline comment, with date, patchset and text.
People taken off the change are under `removed`. To judge whether a
reviewer has gone quiet, read those fields rather than parsing `info` and
`comments --all`; `gc explain reviewers` defines each one.

To find new reviewers, do not tally `git log` by hand:
`gc reviewer-candidates <url> --repo <lustre-release checkout> --ref
upstream/master` (add `--table` for one text row per candidate in ranked order, `--compact` for flat JSON) ranks who reviewed (Reviewed-by trailers, counted double)
and who wrote the touched files over the last 12 months, leaves out the
owner, the commit author and bots, and marks who is already on the change
(`on_change`). Fetch the ref first so the history is current. The ranking
is evidence, not the choice: weigh `maintainers` (who MAINTAINERS names for
the files, beside the ranking: the file is stale, so it adds no one and
skips no hold or cap), recency (`last_reviewed`)
and the five-person cap; a `review_share` near 1 is the gatekeeper who
lands everything, not a file expert. `paths_without_history` lists new
files: rank their directory with `--path`.

Add `--load` to weigh the listed candidates by the review they already
carry (Gerrit's queue of recent changes waiting on their vote against the
votes they cast lately), and `--asked FILE` for your own earlier asks per
person (Patch Watcher's runs are given one): the list is then in
`balanced` order, and `hold` says why not to ask someone now. Use it when
you add reviewers often, so the same few experts are not asked every time.
`--compact` prints only the fields the choice needs (who, score,
balanced, load, asks, hold, and who is on the change now), a fraction
of the full output: read it directly, no jq needed.

## Lustre commit-message rules

These are enforced by the tree's `commit-msg` hook, and a patch that
violates them cannot be pushed:

- Subject `LU-nnnnn component: short description`, 62 columns at most. Use
  `LU-0000` when no ticket has been given.
- Body wrapped at 70 columns at most (the commit-msg hook's limits are
  62 and 70; an older message amended may fail them on lines you did not
  touch). ASCII only: no em dashes, no curly
  quotes. Reviewers also object to `--` used as a dash, so use none:
  a comma, a colon, parentheses or a new sentence instead.
- The signoff section must be contiguous: no blank lines between
  trailers.
- **Never `Co-Authored-By:`**: the hook rejects it. Credit an agent with
  `Assisted-by: ClaudeCode:<model-id>` instead, with the id of the model
  actually running.
- Accepted trailers: `Assisted-by`, `Build-Parameters`, `Change-Id`,
  `CoverityID`, `Fixes`, `Linux-commit`, `Lustre-change`,
  `Lustre-commit`, `Signed-off-by`, `Test-Parameters`, plus the
  name+email ones (`Acked-by`, `Tested-by`, `Reported-by`, `Reviewed-by`,
  `Suggested-by`, `CC`). Run `.git/hooks/commit-msg --help` for the
  authoritative list.
- On amend, preserve the existing `Change-Id`. Never invent one, and
  never add one to a new commit -- the hook generates it.
- `Test-Parameters` lines (`testlist=`, `clientdistro=`, `env=` and the
  like) may go on any patch, to run the tests or distros that exercise
  it. Their sessions run in addition to the default testing, not in
  place of it: only `trivial` and `fortestonly` cut the default back.
  `Test-Parameters: trivial`, which cuts CI back to a minimal
  run, is restricted: use it on test-only patches, or on a patch whose
  only kernel change is to debug messages (the text, arguments or mask
  of a `CDEBUG`, `CERROR` and the like).
- Check before committing: `git diff HEAD | ./contrib/scripts/checkpatch.pl`

A commit message that walks through the code is too verbose. Describe
what was wrong and what changed.

## Uploading a patchset

`gc upload` is the commit push. Use it rather than a hand-built
`git push`: it pushes over HTTPS as the `--user` set's account (never an
SSH alias, which pushes as whoever owns the key), keeps the password out
of URLs and command lines, fixes a committer Gerrit would reject, and
refuses to put HEAD on the wrong change.

```bash
gc upload 64086 --dry-run     # every check, nothing pushed
gc upload 64086               # HEAD -> next patchset of 64086
gc upload 64086 --series      # every new commit up to HEAD
gc upload 64086 --repo /abs/path/to/checkout   # from elsewhere
gc upload 64086 --expect-patchset 3   # refuse if it moved on
gc upload --project fs/lustre-release --branch master   # HEAD as a new change
```

An agent acting as a bot pushes only this way. `gitpushwcmaster`, an SSH
URL or any plain `git push` goes up as the operator whatever CLAUDE.md
says for the operator's own work -- so a bot's patchset would appear under
the operator's name. Give `--repo` as an absolute path when the checkout
is not the working directory.

- Name the change you mean to update. The upload is refused unless
  HEAD's `Change-Id:` is that change's, and the error says whose change
  HEAD's Change-Id belongs to. Fix the commit message; do not drop the
  change argument to get past the refusal.
- One commit by default. If the branch-to-HEAD range holds more commits
  Gerrit does not have, the refusal lists each one's SHA, subject and
  Change-Id. Pass `--series` only when all of them are meant to go up,
  each to its own change; the named change may then be any of them.
  Every commit in a series needs a Change-Id.
- A committer email the account has not registered is rewritten to the
  account's own -- on HEAD, or with `--series` on every commit from the
  first such one up. Author and content are untouched, but the SHAs
  change and HEAD moves; `committer_amended` lists each rewrite.
  `--no-amend` refuses instead.
- With `GERRIT_UPLOAD_AUTHOR` set (a bot's credential set), that
  person's commits go up authored and signed off by the bot and are
  then edited back to their author on Gerrit with notify none, so the
  person is not added as a reviewer of their own change. HEAD moves to
  the edited commits; `author_edit` lists them, and a warning says if an
  edit failed. This is the default; upload another way, or with
  `--no-author-edit`, only for a stated reason.
- More than 50 commits between the branch and HEAD is refused as
  `TOO_MANY_COMMITS`: nearly always HEAD is built on another branch
  (`--base`). A series that really is longer goes up with
  `--series --max-commits N`, only when the user asked for it.
- `--expect-patchset N` refuses unless the change is still at patchset N,
  the one the local commit was written to replace. Without it, a patchset
  someone uploaded while you were working is replaced by yours with no
  conflict and no warning -- git never sees their commit, because an
  amend does not have the previous patchset as a parent. Pass it whenever
  something else might be touching the change: any bot, any concurrent
  session. On a refusal, rebase onto the patchset that is now current
  rather than pushing over it. It does not apply to `--series`.
- Patch Watcher (Patrickbot) rebases a change at level shepherd when
  checkpatch says it cannot be cherry-picked, and higher levels upload
  more. When the current patchset is a bot's, on another parent than your
  commit, `warnings` says so: your push would undo its rebase. Check out
  the current patchset (`gerrit checkout`) and redo the edit on it, unless
  moving the base is what you meant.
- Rebased onto `master-next` at a reviewer's request: `--base master-next`;
  the change still targets master.
- A commit-message-only amend -- wording, a `Test-Parameters:` line -- is
  uploaded the same way; Gerrit takes it as a new patchset with no diff.
- With `--series`, every commit in the range goes to the change its own
  Change-Id names, so one carrying another change's Change-Id would update
  that change. Read the `--dry-run` plan -- one line per commit and the
  change it would `update` or `create` -- before a series push.
- A new patchset is an amend of the old one, never a commit on top of
  it. Two commits with one Change-Id between the branch and HEAD are
  refused as `DUPLICATE_CHANGE_ID` (Gerrit's own word for it is "same
  Change-Id in multiple changes", which is not a duplicate change on the
  server); squash them as the message says and upload again.
- The output carries `patchset`, `url` and per-commit `action`
  (`update`, `create`, `none`). A refused push is `PUSH_REJECTED`, with
  Gerrit's reason in the message and, when the cause is known, a hint.

### Private changes

Changes go up public. Upload a private one only when the user asked
for a private patch in so many words -- never because it is a test, a
draft, or looks unfinished; private changes nobody meant to make pile
up unseen.

```bash
gc upload --project fs/lustre-release --branch master --private
gc upload 70236 --private     # next patchset, kept private
gc upload 70236 --public      # next patchset, and make it public
gc upload ... --private --no-ci-reviewers   # private, no Jenkins/Maloo
```

- `--private` pushes with `%private` and names Jenkins
  (`devops@whamcloud.com`) and Maloo (`maloo@whamcloud.com`) as
  reviewers in the same push. A private change is visible only to its
  owner and reviewers, and CI builds and tests only the patchsets it saw
  created, so reviewers added afterwards miss the first patchset.
- `--private --no-ci-reviewers` leaves Jenkins and Maloo out, so CI
  neither builds nor tests the change. Use it only when the user asked
  for a private change without CI. It does not remove CI reviewers the
  change already has.
- A new patchset for a change that is already private is refused
  (`PRIVATE_CHANGE`) without `--private` or `--public`. Ask the user
  which they want; do not pick one to get past the refusal.
- When the user asks for a private change to go public with its next
  patchset: amend the commit message, then `gc upload <change>
  --public`. A message-only amend keeps the votes and does not rerun CI.
- Never put `%private` on a push by hand; `gc upload --private` is the
  only way a private change is made.

## Before pushing

Run the local AI review first -- see the lreview spin-cycle skill. Fixing
findings locally costs one round trip less than having a reviewer or a bot
find them.

Reading needs no credentials: with only `GERRIT_URL` set, `comments`,
`info`, `search`, `diff` and `series-status` work against a public
Gerrit, and a write refuses up front with exit 2 rather than failing at
the server. Replying, voting, pushing and staging need `GERRIT_USER` and
`GERRIT_PASS` -- `./install.sh --status` shows what is set up and
`./install.sh --configure --only gerrit` sets it up. The HTTP password
is generated in Gerrit under Settings > HTTP Credentials; it is not the
web login password.

An anonymous read sees what any logged-out user sees: drafts and private
changes are not in it.

A host may hold more than one Gerrit login -- a personal account and a
bot, say -- as `[alias]` sections in the same `.env`. `gerrit --user
<alias-or-username> ...` picks one, before or after the subcommand.
Every subcommand takes it, reads included, so a run that writes as the
bot can pass it on every call. Without `--user` the default set is
used, which is the one to leave alone unless the task names an account.
`./install.sh --status` lists the sets a host actually has; do not
guess an alias, since a wrong one is an error rather than a fallback.

`GERRIT_CLI_ENV_FILE`, when set, makes `gerrit` read that one file
instead -- a harness uses it to hand an agent the bot's credentials.
`./install.sh --status` does not see such a file; a refused `--user`
lists the sets it does hold.
