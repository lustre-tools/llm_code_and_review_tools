---
name: gerrit-patch-workflow
description: This skill should be used for work on Gerrit changes with the gerrit/gc CLI - "what comments are on this patch", "reply to the review feedback", "address the reviewer comments", "mark that thread done", "shepherd this patch series", "what is the status of my patches", "post a review on this change", "add a reviewer", "check the CI status of my patches", "upload the new patchset", "push this commit to Gerrit", "push this as a private patch", "upload a private change", "rebase and repush the series", "is there already a patch for this", "find the follow-up to this change". Covers comment triage, staged replies, uploading patchsets, multi-patch series sessions, finding related changes, and Lustre commit-message rules.
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
  it. Only `Test-Parameters: trivial`, which cuts CI back to a minimal
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
