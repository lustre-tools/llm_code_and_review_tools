---
name: lustre-ci-triage
description: This skill should be used when a Lustre Gerrit change has failing CI and the question is why, or what to do about it - "my patch failed CI", "what failed on this change", "is this failure mine or known", "triage these test failures", "should I retest", "link this to a bug", "why is this patch Verified-1", "check the Janitor results", "get the console log for this build". Covers the maloo, jenkins and janitor CLIs and the order they are used in.
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

## Decide whether the failure is yours

Do this before touching a retest. Three questions, three commands:

```bash
maloo bugs <test_set_id> --related          # already a known bug?
maloo test-history test_39b --suite sanity --days 30   # flaky in general?
maloo top-failures lustre-master --days 14  # is the branch itself sick?
```

A failure that appears in `test-history` across unrelated changes, or in
`top-failures` for the branch, is pre-existing. A failure in code the patch
touches is the patch's, however flaky the test is elsewhere.

When a known bug covers it, link it rather than retesting -- a linked
failure stops blocking the landing:

```bash
maloo link-bug <test_set_id> LU-12345
maloo link-bug <subtest_id> LU-12345 --type SubTest
```

Raising a new one: `maloo raise-bug` files via Maloo and auto-links.

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

For a build that failed on infrastructure rather than code,
`jenkins retrigger <job> <build>` re-runs it with the same Gerrit event.
When Jenkins has posted a Verified-1 from a flaky build, the accepted fix
is to post `BUILD` as a Gerrit comment, which re-triggers it:

```bash
ssh -p 29418 <user>@review.whamcloud.com gerrit review -m '"BUILD"' <commit-sha>
```

## Janitor for crashes and raw logs

Janitor runs its own tests and keeps console logs, syslog and kernel crash
logs that Maloo does not expose. Reach for it when a test crashed, hung, or
the Maloo failure message is too thin to act on:

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
