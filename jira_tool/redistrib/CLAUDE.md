# JIRA Tool

Use the `jira` CLI for all JIRA operations. Output is JSON by default (compact, one line). Use `--pretty` for human-readable output. Use `--envelope` to include the full ok/data/meta wrapper.

Run `jira describe` for the complete machine-readable API surface, or `jira describe --command <name>` for a single command.

## Common commands

```bash
jira get LU-12345                              # issue details
jira get LU-12345 --comments                   # issue + first 5 comments
jira get LU-12345 --fields summary,status      # specific fields only
jira get LU-12345 --output status              # single field as plain text (no JSON)
jira comments LU-12345                         # comments with pagination
jira comments LU-12345 --limit 20              # more comments
jira search "project = LU AND status = Open"   # JQL search
jira search "project = LU AND text ~ 'foo'" --fields key,summary,status  # compact results, for duplicate checks
jira search "assignee = currentUser()"         # current user's issues
jira comment LU-12345 "text"                   # add a comment
jira create --project LU --type Bug --summary "title"  # create issue
jira update LU-12345 --assignee jdoe           # update fields
jira transitions LU-12345                      # list available transitions
jira transition LU-12345 31                    # transition to new status
jira link LU-12345 LU-456                      # link issues (default: Related)
jira link LU-12345 LU-456 --type Blocks        # link with type
jira attachments LU-12345                      # list attachments
jira subtasks LU-12345                         # list subtasks
```

## When to use JIRA

Use the jira tool proactively whenever the user mentions an issue key, asks about a bug, wants context on a feature, or is working on something that might have related JIRA history. Don't wait to be told — if JIRA context would be useful, go get it.

JQL is powerful for research. Some useful patterns:
```bash
jira search "project = LU AND text ~ 'write mirroring'"     # full-text search
jira search "project = LU AND status = Open AND component = lov"  # by component
jira search "project = LU AND created >= -30d"               # recent issues
jira search "project = LU AND labels = regression"           # by label
jira search "issuekey in linkedIssues(LU-12345)"             # related issues
```

## Performance

**Run multiple jira commands in parallel** when you need several pieces of information. For example, if you need an issue's details, its comments, and a related issue, run all three `jira get` / `jira comments` calls concurrently rather than sequentially. The tool is stateless — every call is independent.

**Parallel search works especially well.** When searching across multiple projects or running several JQL queries, fire them all at once — e.g., searching LU and EX simultaneously rather than one after the other. Response times vary by query, so parallelism often cuts total wait time dramatically.

## Tips

- Issue keys and JIRA URLs both work: `jira get https://jira.whamcloud.com/browse/LU-12345`
- `--pretty` and `--envelope` work in any position: `jira --pretty get LU-12345` or `jira get LU-12345 --pretty`
- Comments default to 5, search defaults to 20 results — use `--limit` to get more
- `jira config test` to verify connectivity
- `jira --help` for all commands, `jira <command> --help` for command-specific options

## Setup

If `jira` is not installed or `jira config test` fails, tell the user and offer to walk them through setup. Read `INSTALL.md` (in the same directory as this file, inside the jira-tool redistributable) for the full installation steps. Work through them interactively: check prerequisites, install the tool, create `~/.jira-tool.json`, and verify with `jira config test` before proceeding. Don't just paste the instructions — do the work.
