---
name: lustre-ci-triage
description: This skill should be used when a Lustre Gerrit change has failing CI and the question is why, or what to do about it - "my patch failed CI", "what failed on this change", "is this failure mine or known", "triage these test failures", "should I retest", "link this to a bug", "why is this patch Verified-1", "check the Janitor results", "get the console log for this build", "is there an LU for this test failure", "has this test been failing elsewhere", "triage this Maloo failure". Covers the maloo, jenkins and janitor CLIs and the order they are used in.
version: 0.1.0
---

# Lustre CI triage

Three separate CI systems report on a Lustre Gerrit change, and each has
its own CLI:

- **Maloo** (`maloo`) -- the enforced test results at testing.whamcloud.com.
  This is what gates landing.
- **Jenkins** (`jenkins`) -- the builds. A Verified-1 with no test results
  usually means the build failed, not a test.
- **Janitor** (`janitor`) -- a second, independent test infrastructure with
  direct access to console logs, syslog and crash data. No credentials
  needed.

All three print JSON to stdout. Add `--envelope` for `{ok, data, meta}`.
Do not use `--pretty`, and do not pipe output through Python. Run
`<tool> --help` or `<tool> describe` for the full surface; this skill
covers the order to use them in and the judgment that goes with it.

## Cheapest evidence first

Settle each failure from what is already known before downloading
anything. Climb to the next rung only when the one below leaves the
failure genuinely open:

1. **Metadata.** The subtest name and its error message (`maloo
   failures`), the links already on it (`maloo bugs`), a JIRA search,
   `maloo test-history` on the branch and on other reviews, `maloo
   top-failures`, the Janitor's annotation on the subtest (`janitor
   results`), and whether the patch touches the failing code or test
   (`git show --stat HEAD`). None of it downloads a log, and it settles
   most failures.
2. **Logs, targeted.** `maloo logs <test_set_id> --grep <pattern>`,
   `janitor crash`, `janitor fetch ... --grep`, `jenkins console
   --grep`. Grep for the subtest or the error; do not read whole console
   logs end to end.
3. **Reproduction in VMs** (the `ltvm` skill). Only for a failure that
   is plausibly the patch's and that the logs cannot decide: run the
   subtest with and without the patch. Guests are also where a fix is
   tested. They are never the first step: a reproduction costs tens of
   minutes, a `test-history` query seconds.

Failures that share a cause are one question: settle the cause, not
each subtest.

## Start at the change, not at the test

Always begin with the Gerrit-level summary, which separates enforced from
optional results and names the sessions:

```bash
gerrit maloo <change-url-or-number>        # batch: pass several URLs
gerrit maloo 64086 --patchset 3            # a specific patchset
```

Then drill into a failing session:

```bash
maloo failures <session_url>     # failing subtests, with test set IDs
maloo session <session_url>      # session overview (URL or bare UUID)
maloo subtests <test_set_id>     # FAIL only by default; --all, --status PASS
maloo logs <test_set_id> --grep test_81a   # extract suite logs
```

`maloo failures` is the workhorse: it yields both the failing subtest names
and the test set UUIDs that every other command takes.

A failed `test_cleanup` is the one subtest whose `status`, `duration` and
`error` say nothing about the failure: cleanup did not finish, so Autotest
ended the run and reported its own 90-minute budget as `TIMEOUT` /
`"Autotest time out"` / `5400`. It is not a hang. The real error is a
single line in the suite log -- `maloo logs <test_set_id>`, then
`grep -A5 'start cleanup' <suite>.suite_log`. `maloo failures` flags this
in a `note` on the subtest. It is the one failure the metadata cannot
settle, so go straight to that grep.

`maloo logs` extracts what Maloo kept, and Maloo does not always keep every
node's console log. A missing one arrives as a stub of about 66 bytes whose
whole content is "The requested log file ... was not found", and the command
still exits 0. That is normal, not a tool failure -- but a grep over a stub
finds nothing, which reads exactly like a clean log. Before trusting a no-hit,
check which logs are stubs (`grep -l "was not found" console.*.log`) and treat
those nodes' console logs as unavailable.

## Decide whether the failure is yours

Do this before touching a retest. These four steps are the metadata
rung: they need nothing but the subtest name and its error message, both
of which `maloo failures` gives. Go to the logs only when they leave it
open.

**1. Start from the test, its message and any link already on it.**

```bash
maloo bugs <test_set_id>          # links already made, by hand or by signature
```

A link is a lead, not an answer. Check that the ticket describes this test
failing this way (`jira get`) before you rely on it; see mislinks below.

**2. Search JIRA for the full test name, then for the message.**

```bash
jira search 'project = LU AND text ~ "\"sanity test_39b\"" ORDER BY updated DESC' \
	--fields key,summary,status,updated
jira search 'project = LU AND text ~ "\"<distinctive words of the error>\""' \
	--fields key,summary,status,updated
```

Tickets are titled `<suite> test_<n>: <what failed>`, so search on the
full `<suite> test_<n>` phrase. A bare `test_39b` also matches every other
suite's 39b. Strip node names, FIDs, paths and counts from the message
before you search on it. A ticket for the same test with a different
failure mode is a different bug. A closed ticket still counts: the fix may
not be on this branch, or the bug may have come back.

**3. Search Maloo for the subtest: is it failing elsewhere, and on what?**

```bash
maloo test-history test_39b --suite sanity --days 30        # lustre-master
maloo test-history test_39b --suite sanity --days 14 \
	--branch lustre-reviews --limit 30                      # other patches
```

A failure on `lustre-master` (the default) is pre-existing. A failure on
`lustre-reviews` comes from other changes' review testing: the same error
on several unrelated changes is not this patch's. Each entry's `review`
names the Gerrit change and patchset it came from, so you can see which
changes the failure hit (it is null for a branch run). Compare the `error`
field, not just the status, because a test that fails often can fail for
several reasons. Run `maloo bugs <test_set_id>` on the matching failures
to see what others linked them to. That often turns up the ticket step 2
missed. If the test is clean across a busy window and fails only here,
the patch is the likely cause.

**4. Was the test added or changed recently?**

```bash
git log --oneline -s -L '/^test_39b()/,/^}/:lustre/tests/sanity.sh'
git show HEAD --stat -- lustre/tests/        # does this patch touch it?
```

A test added or rewritten in the last few weeks may be failing for its own
reasons. The commit that changed it names an LU ticket, and that ticket is
where to look first. A patch that changes the failing test owns the
failure.

Then check whether the branch itself is failing broadly:

```bash
maloo top-failures lustre-master --days 14
```

A failure in code the patch touches is the patch's, however flaky the test
is elsewhere. Each failure ends up in one of four places: the patch's own,
covered by an existing LU, pre-existing with no ticket (raise one), or
unsettled. For an unsettled failure, say what would settle it: the logs,
or a reproduction with and without the patch.

When a known bug covers it, link it rather than retesting -- a linked
failure stops blocking the landing:

```bash
maloo link-bug <test_set_id> LU-12345
maloo link-bug <subtest_id> LU-12345 --type SubTest
```

`link-bug` reads the link back and reports the state Maloo stored. When
the target already carries a link to that ticket -- usually a pending
one Maloo auto-linked -- Maloo answers OK and leaves it as it was, so
`link-bug` fails with `LINK_STATE_MISMATCH`: the link is still pending
and does not count until someone accepts it in the Maloo web UI. Say so
rather than reporting the failure covered. A `warning` with a null
`state` means the read-back failed; check with `maloo bugs`.

`maloo bugs` on a test set includes the links on its subtests, where most
of them are, and says which subtest each is on; `maloo subtests
<test_set_id>` gives the subtest ids. It takes a test set or subtest id,
not a session id.

Whether a link counts is its `state` (accepted, pending, rejected). The
`status` and `summary` beside it are both Maloo's own copy of the ticket,
taken when the link was made and never refreshed: an Open LU ticket can
read Abandoned there, and a renamed one still shows its old title. Ask
`jira get` for either before acting on it.

Maloo also auto-links by signature, and those mislink. A pending link to
a ticket with nothing to do with the test is ordinary -- read the ticket,
disregard it if it does not fit, and move on. Neither the mislink nor the
stale copy is worth reporting as a tool defect: the API returns only the
ticket, its cached summary and status, and the link state, so there is no
field saying where a link came from and nothing for the CLI to fix.

Raise a new ticket only once the evidence shows the failure is not the
patch's -- the same failure on the branch or on other changes in
`maloo test-history`, or a reproduction without the patch -- and
`maloo bugs` and `jira search` on the test name and the message find
nothing that covers it. `maloo raise-bug <test_set_id> --summary ...
--description ...` files it in LU and links it in one step; link it to
any other failed session with the same cause with `link-bug`. One
ticket per cause, not per subtest. The summary takes the usual form,
`<suite> test_<n>: <what failed>`; the description gives the failure
message, the Maloo links, and the evidence that it is not the patch's.

`link-bug` takes LU tickets only; any other project prefix is refused
("project prefixes may only include LU"). Infrastructure failures --
node-provisioning, `LJBChefError` -- are tracked in DCO tickets, which Maloo
usually auto-links to the failed subtest itself (`maloo bugs` shows
them). For those, skip the link and retest with the DCO ticket:
`maloo retest <session_url> DCO-11677` is accepted. Such a failure is
covered, so do not raise an LU ticket for it. DCO tickets cannot be read
with the `jira` CLI here; go by the summary `maloo bugs` gives.

## Retest only what deserves it

```bash
maloo retest <session_url> LU-19487              # single session
maloo retest <session_url> LU-19487 --option all
maloo queue --review <change>                    # watch progress
```

Rules that matter:

- Retest **enforced** failures only. An optional-suite failure does not
  block landing and burns CI capacity to re-run.
- A retest needs a JIRA ticket as justification -- that is the argument,
  not a formality.
- Retests take 10-30+ minutes. Do not resubmit until 60+ minutes have
  passed; a second request does not make the first go faster.
- Retest pre-existing failures. A failure caused by the patch will fail
  again.

## Build failures go to Jenkins

A Verified-1 with no Maloo session is a build:

```bash
jenkins review 64086                       # builds for a change
jenkins build lustre-reviews lastFailedBuild
jenkins console lustre-reviews 121880 --grep error --tail 200
jenkins run-console <job> <build> <run>    # one matrix sub-build
```

A build failed on infrastructure when the builder died, lost its agent,
or ran out of disk, with no compile, packaging or test error the patch
could have caused. A compile error in files the patch does not touch,
on a base weeks behind the branch, is usually kernel compatibility the
branch has since fixed: the repair is a rebase, not a source change.

For a build that failed on infrastructure rather than code,
`jenkins retrigger <job> <build>` re-runs it with the same Gerrit event.
When Jenkins has posted a Verified-1 from a flaky build, the accepted fix
is to post `BUILD` as a Gerrit comment, which re-triggers it:

```bash
ssh -p 29418 <user>@review.whamcloud.com gerrit review -m '"BUILD"' <commit-sha>
```

## Janitor for crashes and raw logs

Janitor runs its own tests and keeps console logs, syslog and kernel crash
logs that Maloo does not expose. Its results are not enforced: a patch can
land with Janitor failures standing, there is no retest, and no Maloo
session to link a ticket to.

Start with the annotation `janitor results` puts beside each failed
subtest -- `test_1c(810 fails in 30d)` fleet-wide, `Seen in reviews:`
with other changes' numbers, or `NEW unique failure`. A subtest failing
across the fleet or on other reviews is not this patch's unless the patch
touches that code; a new unique failure is the one to look at. Reach for
its logs when a test crashed, hung, or the failure message is too thin to
act on:

```bash
janitor results 64440                          # change number, build number or URL
janitor detail 61009 "sanity2@ldiskfs+DNE"     # per-subtest from results.yml
janitor crash 61009 "sanity3@zfs" -C 5         # LBUG/LASSERT/panic/oops with context
janitor logs 61009 "sanity2@ldiskfs+DNE"       # what files exist
janitor fetch 61009 "sanity3@zfs" console.txt --grep LBUG
```

Build and change numbers overlap; a bare number that could be either is
refused, so pass `--build` or `--change` when it complains.

## What the failure shape tells you

- **LBUG, LASSERT, kernel panic, oops** -- a crash. Get the signature from
  `janitor crash`, then search for it in JIRA (`jira search`) before
  assuming it is new. If a vmcore is involved, switch to the crash triage
  skill.
- **Test timeout or hang** -- look for D-state tasks and stuck RPCs in the
  console log; `janitor fetch ... syslog` usually has more than the suite
  log.
- **One subtest, clean elsewhere** -- check `maloo test-history` first;
  most are known flakes with an LU already.
- **Every suite failing** -- suspect the build or the branch, not the
  patch. Check `jenkins review` and `maloo top-failures`.

## Reporting back

State which failures are enforced, which are pre-existing (with the LU
number), and which are the patch's own. Never describe a patch as "CI
clean" on the strength of optional suites, and never claim a retest was
requested without the command's result confirming it.

`maloo` needs credentials; the tools installer writes them
(`./install.sh --status` shows what is configured, `./install.sh
--configure --only maloo` sets one up). Janitor needs none, and neither
does reading from `jenkins` -- only `jenkins abort` and `jenkins
retrigger` do, and they say so and exit 2 when unconfigured.

Both take `--user <alias-or-username>` when a host holds more than one
account, in any argument position. On `jenkins` that flag names a stored
credential set, not a bare username: `--token` is still the way to pass
a token by hand. Leave `--user` off unless the task names an account --
`./install.sh --status` lists what a host has. A host with no Jenkins
set at all refuses `jenkins --user` even for a read, so leave it off
reads.

`MALOO_TOOL_ENV_FILE` and `JENKINS_TOOL_ENV_FILE`, when set, make each
tool read that one file instead -- a harness uses them to hand an
agent the bot's credentials. `./install.sh --status` does not see such
a file; a refused `--user` lists the sets it does hold.
