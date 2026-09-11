---
name: gerrit-patch-workflow
description: This skill should be used for work on Gerrit changes with the gerrit/gc CLI - "what comments are on this patch", "reply to the review feedback", "address the reviewer comments", "mark that thread done", "shepherd this patch series", "what is the status of my patches", "post a review on this change", "add a reviewer", "check the CI status of my patches", "rebase and repush the series". Covers comment triage, staged replies, multi-patch series sessions, and Lustre commit-message rules.
version: 0.1.0
---

# Gerrit patch workflow

`gerrit` (aliases `gc`, `gerrit-cli`) covers review comments, patch series
sessions, change metadata and CI overview. Output is JSON; add
`--envelope` for the full wrapper, skip `--pretty`. `gc examples` prints
the built-in workflow cheatsheets, `gc explain <command>` details one
command, and `gc describe` emits the machine-readable API.

A change is addressed by full URL or by bare number. Most commands
remember the URL from the last `gc comments`, so later calls can omit it.

## Reading feedback

```bash
gc comments <url>                 # unresolved threads, with code context
gc comments 64086 --all           # include resolved
gc series-comments <url>          # every patch in the series at once
gc comments 64086 --fields=index,file,message
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
gc reply 1 "Fixed in PS4"
gc reply 2 --ack
gc done <url> <idx>               # same as reply --done
gc ack <url> <idx>
```

For several replies at once, stage them, look at them, then push:

```bash
gc stage --done 0
gc stage 1 "Will fix in a follow-up"
gc staged list
gc push --dry-run <change-id>
gc push <change-id>
```

`gc push` posts staged **comment replies**. It does not push commits;
git does that, to `refs/for/<branch>`.

Declining a finding is a legitimate outcome -- reply with why. Do not
silently skip a comment, and do not answer a finding by adding an
explanatory code comment (see the lreview spin-cycle skill for why that
habit wrecks a patch).

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
gc diff <url>
gc maloo <url>            # enforced/optional test summary
gc watch patches.json     # Maloo triage across a watched list
```

`gc watch` takes a JSON array of objects with a `gerrit_url` field. For
what to do with a failure, use the CI triage skill.

## Change metadata

```bash
gc vote <url> <label> <score>        gc message <url> "text"
gc set-topic <url> <topic>           gc hashtag <url> --add <tag>
gc rebase <url>                      gc checkout <url>
gc abandon <url>                     gc restore <url>
gc reviewers <url>                   gc add-reviewer <url> <name>
gc find-user <name>                  gc remove-reviewer <url> <name>
```

`add-reviewer` and `find-user` do fuzzy name matching -- confirm the match
before adding someone to a change.

## Lustre commit-message rules

These are enforced by the tree's `commit-msg` hook, and a patch that
violates them cannot be pushed:

- Subject `LU-nnnnn component: short description`, under 64 columns. Use
  `LU-0000` when no ticket has been given.
- Body wrapped to 60 columns. ASCII only -- no em dashes, no curly
  quotes; write `--`.
- The signoff section must be contiguous: no blank lines between
  trailers.
- **Never `Co-Authored-By:`** -- the hook rejects it. Credit an agent with
  `Assisted-by: AGENT_NAME:MODEL_VERSION` instead, e.g.
  `Assisted-by: ClaudeCode:claude-opus-5`.
- Accepted trailers: `Assisted-by`, `Build-Parameters`, `Change-Id`,
  `CoverityID`, `Fixes`, `Linux-commit`, `Lustre-change`,
  `Lustre-commit`, `Signed-off-by`, `Test-Parameters`, plus the
  name+email ones (`Acked-by`, `Tested-by`, `Reported-by`, `Reviewed-by`,
  `Suggested-by`, `CC`). Run `.git/hooks/commit-msg --help` for the
  authoritative list.
- On amend, preserve the existing `Change-Id`. Never invent one, and
  never add one to a new commit -- the hook generates it.
- `Test-Parameters` belongs only on test-only patches.
- Check before committing: `git diff HEAD | ./contrib/scripts/checkpatch.pl`

A commit message that walks through the code is too verbose. Describe
what was wrong and what changed.

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
