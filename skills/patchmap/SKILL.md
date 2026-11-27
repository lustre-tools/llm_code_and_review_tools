---
name: patchmap
description: This skill should be used to find or record which agent session owns a patch, with the patchmap CLI - "which session is working on 71234", "who owns this patch", "what patches does this session have", "which worktree is LU-12345 in", "record this patch in patchmap", "update my patchmap rows", "patchmap show", "list the stale patchmap rows", "remove the landed patches from patchmap". Covers reading the map before searching transcripts, when to set and update rows, the state text, links to the patch and ticket, session names, and a map kept on another host.
version: 0.1.0
---

# patchmap: which session owns which patch

Many agent sessions run at once, each on its own patches, and a person
tracking them needs to know which session, worktree and branch holds a
patch, and how far along it is.  `patchmap` keeps that in one file,
`~/.claude/patchmap.yaml` (`PATCHMAP_FILE` moves it): one line per
patch, with its change number or PR, ticket (an LU or any other), short description,
branch, worktree, host, owning session and state.

Read it before searching session transcripts for who owns a patch.  It
answers in milliseconds, and a transcript search does not.

```bash
patchmap show                     # every row, with its session's name
patchmap show --mine              # this session's rows
patchmap show --session "ext4"    # one session's, by id prefix or name text
patchmap show --stale             # rows whose worktree on this host is gone
patchmap show --json              # the same, for a program
patchmap get 71234                # one row, by number, worktree or branch
```

`show` prints each patch as `NNNNN ("LU-nnnnn few words")`, as a
`Fixes:` line names a commit, the form for naming a patch to a person, with the session's name beside it and, when
the config gives templates, links to the patch and its ticket.  On a
terminal it pages through `$PATCHMAP_PAGER`, `$PAGER` or `less`, as git
does (`--no-pager` to skip); piped, as an agent runs it, it just prints.

## Keep your own rows current

Write the file only through `patchmap set` and `patchmap rm`.  They take
a lock, so sessions writing at the same moment do not lose each other's
rows.  Never edit it by hand.

```bash
# starting a patch: the row is found again later by --dir
patchmap set --dir ~/src/x.wt/LU-12345-foo --ticket LU-12345 \
    --desc "few word description" --state "local, unpushed"
# pushed: the number joins the row found by its worktree
patchmap set --dir ~/src/x.wt/LU-12345-foo --patch 71234 --state "PS1 in CI"
# later, by number
patchmap set --patch 71234 --state "PS2: review +1, waiting on CI"
patchmap set --patch 71234 --state "landed"   # or: patchmap rm 71234
```

- **When to set it.**  When you start a patch, when you push a patch
  set, when it lands or is abandoned, when its worktree is removed, and
  each time you post a status summary.  A row that says "PS1 in CI"
  two days after landing is worse than no row.
- **The state** is a short phrase a person can act on: the patch set, then
  what it waits on (`PS3 in CI`, `local, tested, awaiting push OK`,
  `PS2 -1 from reviewer, fixing`).  Do not just write `in progress`.
- **Unpushed patches** have no number: their `patch` is `new` and the row
  is found by `--dir` or `--branch`.  `new` is never a key, so `get new`
  and `rm new` match nothing.
- **The patch key** is a change number on the default tracker (`71234`),
  `TRACKER:N` for a change on another one (`gerrithub:1234`), or
  `owner/repo#N` for a GitHub PR.  A Gerrit number is unique on its
  server, so changes to every project on the default Gerrit (fs/lustre-release,
  tools/e2fsprogs, ...) are bare numbers.
- **The ticket** (`--ticket`, or its old name `--lu`) is the work item:
  `LU-nnnnn`, or anything a `[tickets]` prefix below matches.
- **Another session's patch** is not yours to update.  Ask that session,
  or tell the person.  A `set` on its row (say, to mark it landed) keeps
  its owner, says so on stderr, and records you as `updated_by` (a
  session id, or `user@host` from a shell), which `show` prints as
  "updated ... by ..." until the owner next updates the row; only `--take`, or `--session ID`, moves
  the row to another session, and drops the old session's name.  A
  desktop-app session that resumes under a new session id still owns its
  rows.
- `--clear FIELD` drops one field.  `rm` prints what it removed, so a
  mistake can be put back with `set`.

## Links

`~/.config/patchmap.toml` (`PATCHMAP_CONFIG` moves it) maps trackers and
ticket prefixes to URL templates.  `show` prints the links under each row,
and `show --json` gives them as `url` and `ticket_url`.  With no file
there are no links; nothing else changes.  For Lustre work:

```toml
[trackers]
default = "https://review.whamcloud.com/{n}"      # bare numbers
github = "https://github.com/{repo}/pull/{n}"     # owner/repo#N
# gerrithub = "https://review.gerrithub.io/{n}"   # gerrithub:N

[tickets]
"LU-" = "https://jira.whamcloud.com/browse/{id}"  # longest prefix wins
"EX-" = "https://jira.whamcloud.com/browse/{id}"
```

A template that names a field the key does not have gives no link
rather than an error.

## Session names

`set` records the session it runs in, from `$CLAUDE_CODE_SESSION_ID` (and
the desktop app's `$CLAUDE_CODE_HOST_SESSION_ID`), and stores the
session's name with it.  `show` looks each name up again, so a renamed
session shows its new name.  It looks in this order:

1. the Claude desktop app's record of the session (macOS): its sidebar
   title, and whether it is archived;
2. the session's Claude Code transcript under `~/.claude/projects/`, on
   any host: its last `/rename` title, else the title Claude Code gave it;
3. the name `set` stored, for a session on another host.

`patchmap title` prints this session's name as `show` will.  Headless runs
(`claude -p`, lreview's reviewers) have no name, and show as a bare id.

## Patches on another host

The map lives on one host.  The usual way to track a patch elsewhere is
from a session on the map's host that works on the other host over ssh:
it records the row with `--host <that host>` and the path there as
`--dir`.

`PATCHMAP_REMOTE=<map host>` is the other way round, and off unless set:
a session on another host runs patchmap there, which fills in that host,
the session and its name, and the absolute worktree path and branch, and
runs the command on the map host over ssh.  It needs that host to be able
to log in to the map's host, with `patchmap` on the PATH an ssh command
gets (`PATCHMAP_REMOTE_CMD` names another).  Do not set it up where the
map's host is the more trusted of the two, such as a personal machine
driving a shared build or test host that others have root on: ask the
person first, and use `--host` from the map's host instead.

`--stale` and the `[dir gone]` flag only check worktrees on the host
`show` runs on.
