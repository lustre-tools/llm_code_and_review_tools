# Maloo Tool

A thin, LLM-agent-focused CLI for the Maloo Lustre CI test results system
(`https://testing.whamcloud.com`).

## Installation

```bash
pip install -e .
```

## Configuration

Set environment variables:

```bash
export MALOO_USER="your-username"
export MALOO_PASS="your-password"
```

The server URL defaults to `https://testing.whamcloud.com`.

## ID Types

Maloo uses two distinct UUID types:

- **Session UUID** — identifies a full test session (used with `session`, `failures`, `review`)
- **Test set UUID** — identifies a suite run within a session (used with `subtests`, `bugs`, `logs`)

Both can be found in the output of `session` and `failures`.

## Quick Start

```bash
# Session overview: suites with pass/fail counts
maloo session <session-UUID>

# Drill into failures: failed subtests with error messages
maloo failures <session-UUID>

# All subtests for a suite (default: FAIL only; use --all for everything)
maloo subtests <test_set-UUID>
maloo subtests <test_set-UUID> --status PASS

# Bug links for a test set
maloo bugs <test_set-UUID>

# Find test sessions for a Gerrit review
maloo review 54225

# List recent sessions
maloo sessions --branch lustre-master
maloo sessions --branch lustre-master --failed --days 14
maloo sessions --host onyx-53vm1 --days 3

# Most common failures on a branch
maloo top-failures lustre-master --days 7 --limit 10

# Pass/fail history for a specific test
maloo test-history test_39b --suite sanity --days 30
maloo test-history test_1b --branch lustre-reviews

# Queue status
maloo queue --branch lustre-master
maloo queue --review 54225

# Download test logs (optionally grep inside)
maloo logs <test_set-UUID>
maloo logs <test_set-UUID> --grep "test_81a"
```

## Output Format

All commands return JSON by default. Add `--pretty` for human-readable formatting:

```bash
maloo session <uuid> --pretty
maloo failures <uuid> --pretty
```

The envelope format:

```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "tool": "maloo",
    "command": "session",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

## Commands

### Session and Failures

| Command | Description |
|---------|-------------|
| `maloo session <session-UUID>` | Session overview: suites, pass/fail totals |
| `maloo failures <session-UUID>` | Failed subtests with error messages for each failed suite |
| `maloo subtests <test_set-UUID>` | All subtests for a suite (filter by `--status`) |

### Bugs and Retesting

| Command | Description |
|---------|-------------|
| `maloo bugs <test_set-UUID>` | JIRA bug links for a test failure |
| `maloo link-bug <test_set-UUID> <TICKET>` | Associate a JIRA bug with a test failure |
| `maloo retest <session-URL> <TICKET>` | Request a retest (requires JIRA justification) |

### Searching and History

| Command | Description |
|---------|-------------|
| `maloo sessions` | List recent sessions (filter by `--branch`, `--host`, `--failed`) |
| `maloo review <change>` | Test sessions for a Gerrit change number |
| `maloo top-failures <branch>` | Most common failing tests on a branch |
| `maloo test-history <test>` | Pass/fail history for a specific subtest |
| `maloo queue` | Current test queue (filter by `--review`, `--branch`, `--status`) |

### Logs

| Command | Description |
|---------|-------------|
| `maloo logs <test_set-UUID>` | Download and extract test logs (optionally `--grep PATTERN`) |

## LLM Context Awareness

- `sessions` defaults to last 7 days and 20 results; use `--days` and `--limit` to adjust
- `subtests` defaults to `--status FAIL`; use `--all` to see every subtest
- `test-history` defaults to 14 days and failures only; use `--all` to include passes
- `top-failures` scans up to 50 sessions by default; adjust with `--sessions N`
- `logs` downloads to `/tmp/maloo_logs` by default; use `--output-dir` to change

## License

MIT
