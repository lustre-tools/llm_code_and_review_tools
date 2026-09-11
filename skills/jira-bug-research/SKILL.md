---
name: jira-bug-research
description: This skill should be used for JIRA work with the jira CLI - "look up LU-12345", "is there a bug for this failure", "search JIRA for this assertion", "file a bug for this", "comment on that ticket", "link these two issues", "who is this assigned to", "what LU covers this test failure", "move this ticket to In Progress". Covers JQL search for known-issue research, multi-instance and Cloud routing, and issue creation and linking.
version: 0.1.0
---

# JIRA bug research and issue work

`jira` is the CLI for bug tracking and test-failure research. Output is
JSON on stdout; add `--envelope` for `{ok, data, meta}`, skip `--pretty`,
and do not pipe it through Python. `jira describe` emits the full
machine-readable API; `jira --help` lists all commands.

Exit codes are meaningful: 0 success, 1 general error, 2 auth, 3 not
found, 4 invalid input, 5 network.

## Research before filing

Most Lustre test failures already have an LU. Search before creating
anything, and search on the failure's own words -- the assertion text, the
function name, the test name:

```bash
jira search "project = LU AND text ~ 'sanity test_39b'" --limit 20
jira search "project = LU AND text ~ 'LBUG.*ldlm_lock_decref'" --fields key,summary,status
jira search "project = LU AND status != Closed AND text ~ 'osc_extent'" --output key
```

`--output <field>` prints one field per line, which is what to use when
feeding keys into another command. `--fields` trims the payload.

Then read the candidates:

```bash
jira get LU-12345
jira comments LU-12345
jira links LU-12345          # what it relates to, duplicates, blocks
jira subtasks LU-12345
```

A ticket that names the same assertion in the same function is the same
bug even when the reproducer differs. A ticket with the same test name but
a different failure mode is not -- say which one it is rather than linking
on a name match.

## Filing and updating

```bash
jira create --project LU --type Bug --summary "..." --description "..."
jira comment LU-12345 "..."
jira update LU-12345 --summary "..."
jira transition LU-12345 "In Progress"
jira transitions LU-12345          # what states this issue can move to
jira assign LU-12345 <user>
jira link LU-12345 LU-12346 --type Duplicate
jira link-types                    # what link types exist
```

Check `jira transitions` before a `jira transition`: the available states
depend on the workflow and the issue's current state, and guessing
produces a 400 rather than a helpful error. Same for `jira issue-types`,
`jira components`, `jira versions` and `jira roles` when creating or
editing.

Labels, components and fix versions each have add and remove commands
(`add-label` / `remove-label`, `set-component` / `remove-component`,
`set-fix-version` / `remove-fix-version`). The `set-*` ones add and keep
what is already there.

## Connecting a failure to a bug

When triaging CI, the link goes both ways: find or file the LU here, then
record it on the test result with `maloo link-bug <id> LU-12345` so the
failure stops blocking the landing. See the CI triage skill. A retest
also requires a ticket as its justification.

For the commit that fixes it, the subject line is `LU-nnnnn component:
short description`; use `LU-0000` when no ticket has been given.

## Instances and Cloud routing

One `jira` can talk to several instances:

- `-I <name>` selects a named instance from `~/.jira-tool.json`.
- Without `-I`, projects listed in `JIRA_CLOUD_PROJECTS` route to the
  Cloud instance automatically, by project prefix extracted from the issue
  key or the JQL. Everything else goes to the default instance.
- An explicit `-I` always overrides auto-routing.

```bash
jira get LU-20002              # default instance (Whamcloud server)
jira -I cloud get EX-13727     # named instance
```

Cloud and Server differences are handled transparently -- REST v3 versus
v2, Atlassian Document Format conversion on read and write, `accountId`
instead of `username`, `nextPageToken` pagination. Display names are
resolved to account IDs automatically for `assign`, `watch` and `unwatch`;
use `jira users <name>` to check who a name resolves to before assigning.

## Configuration

The single-instance path is `JIRA_SERVER` and `JIRA_TOKEN`, written by
`./install.sh --configure --only jira` into `~/.config/jira-tool/.env`.
There is no username: the token is the whole login.

The token is only needed to write. Against a public Jira, `get`,
`search` and `comments` work with `JIRA_SERVER` alone -- so research
needs no account, while filing, commenting, linking and transitioning
refuse up front with exit 2 until there is one.

Multi-instance configuration lives in `~/.jira-tool.json`, with an
`instances` map and a `default`. Cloud routing additionally reads
`JIRA_CLOUD_SERVER`, `JIRA_CLOUD_EMAIL`, `JIRA_CLOUD_TOKEN` and
`JIRA_CLOUD_PROJECTS` from the environment, not from a config-file
instance.

Tokens: a Server personal access token comes from the JIRA profile menu
(Profile > Personal Access Tokens); a Cloud API token from
`id.atlassian.com/manage-profile/security/api-tokens`.
`./install.sh --status` reports which of these is actually in place.

## Reporting

When answering "is this a known bug", give the key, the summary, the
status, and the reason it is or is not the same failure. An LU number with
no statement of why it matches is not an answer.
