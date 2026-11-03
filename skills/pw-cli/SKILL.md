---
name: pw-cli
description: This skill should be used to ask Patch Watcher (the Lustre Gerrit bot, @Patrickbot) for what tagging it on a Gerrit change asks, with the pw-cli CLI and your own Gerrit login - "have the bot retest my patch", "get Patch Watcher to shepherd this change", "watch 12345 at bigfix", "is the bot watching this change", "what level is this change at", "stop the bot on my change", "halt the bot on this patch", "release my halt", "have the bot find reviewers", "run an lreview on this change", "ask the bot to rebase this", "send the bot feedback". Covers what each level does, which changes retest and shepherd refuse, who may do what, and the daily limits.
version: 0.2.0
---

# Patch Watcher by Gerrit login

Patch Watcher is the bot (`@Patrickbot` on review.whamcloud.com) that
watches Lustre changes. `pw-cli` asks it, as your Gerrit account, for
what tagging it on a change asks, without writing the comment. The same
rules decide what you may do as decide what your tags may do. Backlog work
is not offered here; tag the bot for that.

```bash
pw-cli whoami                  # your account, role, and what you may do
pw-cli status 69157            # watched? at what level? halted, and why?
pw-cli retest 69157            # watch at retest
pw-cli shepherd 69157 69158    # watch at shepherd, several at once
pw-cli watch 69157 --level bigfix   # any level your role allows
pw-cli stop 69157              # stop watching, and stop its run
pw-cli halt 69157 owner is reworking this
pw-cli clear-halt 69157 --note "rework uploaded"
pw-cli reviewers 69157         # find and add reviewers, once
pw-cli lreview 69157 [--light] # AI review posted on the patchset
pw-cli request 69157 rebase this on master and fix the conflict
pw-cli request 69157 -         # the request from stdin, paragraphs and all
pw-cli feedback 69157 the bot retested a failure it caused
```

A change is a number or any Gerrit URL for it. Output is JSON on stdout;
`--envelope` gives `{ok, data, meta}`. Exit codes: 0 done; 1 refused or
failed (the error's `code` says which: `NOT_AUTHORIZED`, `CONFLICT`,
`RATE_LIMITED`, `SERVER_ERROR`, with the server's own reason in
`details.server_code`); 2 the login is missing or Gerrit refused it; 3 no
such change, or one Gerrit does not show you; 4 bad arguments; 5 Patch
Watcher could not be reached. With several changes, one refusal does not
stop the rest: each result is in the list, a refused one as
`{"change", "error"}`, and the exit code is the first refusal's.

## The login

It uses gerrit-cli's credentials: `GERRIT_USER` and `GERRIT_PASS` from the
environment or `~/.config/gerrit-cli/.env`, and `--user SET` picks another
credential set there, as with `gerrit --user`. The password is the Gerrit
**HTTP** password (Gerrit: Settings > HTTP Credentials). Patch Watcher
checks it with Gerrit on every new login and remembers a good one for ten
minutes; it never stores the password. Each change is read with your own
login, so a change Gerrit does not show you is "not found" here too.

If `whoami` says `AUTH_FAILED`, the HTTP password is wrong or has been
regenerated: fix the `.env`, do not retry in a loop. After five refused
logins for one name in fifteen minutes the server stops asking Gerrit and
answers `RATE_LIMITED` until the quarter hour is up.

`PW_CLI_URL` points it at another Patch Watcher; the default is
`https://patchwatcher.mulberrytree.us`. It must be https (or loopback),
since the Gerrit password goes with every request.

## Who may do what

`whoami` says your `role`, the levels you may set (`may_watch_at`),
whether you may stop changes others had watched (`may_stop_others`), and
whether you may start runs (`may_ask_for_runs`).

- **Anyone with a Gerrit account** (a *retest tagger*): watch a change at
  retest or shepherd, or move it between them if nobody set it higher;
  stop, lower or halt a change they had the bot watch; release a halt
  they set; give feedback; see the status of any change Gerrit shows them.
  Those whose Gerrit email is at ddn.com or whamcloud.com may also stop or
  lower a change someone else had watched at shepherd or below.
- **Requesters and admins** (Patch Watcher's taggers, named by its
  operator): any level, on any change; stop, halt and release any; and
  `reviewers`, `lreview` and `request`.
- **Anyone** may stop a change the bot took from someone else with a JIRA
  ticket, as the bot's notice on that change says.

`status` shows any change Gerrit shows you, whoever watches it, and says
with `may_change` and `halt.you_may_clear` what you could do about it.

## What the levels do

- **retest**: retests enforced CI failures linked to an existing LU
  ticket, and sends failures no ticket covers to an AI run, which links and
  retests known ones, files tickets for ones that are not the change's own,
  and reports on the rest. Never uploads to the change.
- **shepherd**: retest, and once CI passes, adds reviewers if too few are
  active, pings them at 7 and 21 days, and adds a second set after 14 quiet
  days. It adds them 16 hours after Maloo's +1, once the patchset's AI review
  is in (24 hours at most), not while that review found concerns, and never
  on a change whose subject starts with WIP; it says on the change whom it
  added. Its only upload is a simple rebase (a cherry-pick veto, or a patchset
  over 30 days old): when the rebase needs no code changes, or only
  mechanical substitutions, it uploads and says so on the change; any other
  conflict is left for a person.
- **smallfix**: repairs builds, rebases through conflicts, and makes small
  changes reviewers ask for.
- **bigfix**: makes everything reviewers ask for; design-level changes wait
  for the owner.
- **own**: as bigfix, without waiting on design changes.

Retest and shepherd are refused (`CONFLICT`, `server_code: level_refused`,
with the reason) on a change not meant to land as it stands: more than four
deep in its series of open changes, against LU-0000, or marked
`fortestonly` or `forbuildonly`. Do not retry those. Smallfix and up still
work on them.

## Runs: reviewers, lreview, request

Each starts one run at once and answers on the change itself:

- `reviewers` adds up to three reviewers from MAINTAINERS and the files'
  history. A change gets at most two sets; a third is declined.
- `lreview` posts AI review findings on the current patchset as inline
  comments; 30 to 90 minutes, `--light` is quicker. It never votes.
- `request TEXT` starts one run that does what TEXT asks and nothing else.
  The words are posted on the change, and the run answers there when it
  ends. It declines a request it finds unreasonable or unrelated. Not on a
  closed or halted change; `CONFLICT` with `server_code: busy` when another
  run is on the change (ask again when it ends).

A person may start 10 request runs a day, on Gerrit, JIRA and here
together; admins are not limited. Write the request as you would for a
colleague: what to do, on what, and what done looks like.

## Limits worth knowing

- `RATE_LIMITED`, `server_code: daily_limit`: 10 requests a day (above);
  30 changes newly watched a day for someone who is only a retest tagger
  (changing a level already set does not count); 5 feedback notes a day.
- `NOT_AUTHORIZED`: the role does not allow it. Lowering, stopping or
  halting someone else's change, or a level above shepherd, takes a
  requester or admin; ask whoever watched it, or Patrick Farrell to be
  added.
- `CONFLICT`, `server_code: closed`: the change is merged or abandoned.

## Judgment

- Check `status` first when you do not know how the bot has the change: a
  change already at the level you want needs nothing.
- Watch what you mean to land. Retest on a change about to be reworked
  spends CI on results nobody reads; halt or stop it instead.
- Do not raise a change someone else owns to smallfix or above without
  their say: the bot will upload to it.
- `halt` needs a reason a person will understand later. A halt stops the
  change's run and keeps the bot off it until released; `stop` takes it off
  the watch list.
- For queued, non-urgent work not tied to one change, tag the bot with
  `watch backlog` on Gerrit or use `pw-admin`; this tool does not queue
  backlog work.
