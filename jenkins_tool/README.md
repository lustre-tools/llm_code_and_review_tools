# Jenkins Tool

A thin, LLM-agent-focused CLI for the Jenkins build server. Query build status,
console output, and Gerrit-triggered builds.

## Installation

```bash
pip install -e .
```

## Configuration

`JENKINS_USER` and `JENKINS_TOKEN` are required; `JENKINS_URL` is
optional and defaults to `https://build.whamcloud.com`:

```bash
export JENKINS_URL="https://build.whamcloud.com"   # optional
export JENKINS_USER="your-username"
export JENKINS_TOKEN="your-api-token"
```

Instead of exporting variables, the settings may be placed in a `.env`
file — the first existing of `~/.config/jenkins-tool/.env`,
`/shared/support_files/.env`, or `./.env` is loaded (only that one
file; values do not override variables already set in the environment).
Every command also accepts `--url`, `--user`, and `--token` options
which override both env vars and `.env` values.

## Quick Start

```bash
# List all jobs with health status
jenkins jobs
jenkins jobs --view lustre

# List recent builds for a job
jenkins builds lustre-reviews --limit 10

# Build details + per-config status (matrix jobs)
jenkins build lustre-reviews 121881
jenkins build lustre-master lastFailedBuild

# Console output (last 200 lines by default)
jenkins console lustre-reviews 121880
jenkins console lustre-master 4704 --grep "error"

# Sub-build console for a specific matrix configuration
jenkins run-console lustre-reviews 121880 "arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel"

# Find builds for a Gerrit change number
jenkins review 54225

# Abort a running build (and all its sub-builds)
jenkins abort lustre-reviews 121884
jenkins abort lustre-reviews 121884 --kill   # hard-kill

# Retrigger a Gerrit-triggered build
jenkins retrigger lustre-reviews 121880
```

## Output Format

By default, commands output only the JSON data payload (errors output
the error payload). Pass the global `--envelope` flag before the
subcommand (e.g. `jenkins --envelope build lustre-reviews 121881`) to
wrap output in the full envelope:

```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "tool": "jenkins",
    "command": "build",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

Use `--pretty` for human-readable formatted output. It works on any command:

```bash
jenkins builds lustre-reviews --limit 10 --pretty
jenkins build lustre-reviews 121881 --pretty
```

## Commands

| Command | Description |
|---------|-------------|
| `jenkins jobs` | List all jobs with status and health score |
| `jenkins builds <job>` | List recent builds for a job |
| `jenkins build <job> [number]` | Build details with matrix run status |
| `jenkins console <job> [number]` | Console output (tail/head/grep) |
| `jenkins run-console <job> <number> <config>` | Console for a specific matrix sub-build |
| `jenkins review <change>` | Find builds for a Gerrit change number |
| `jenkins abort <job> <number>` | Abort a running build and all sub-builds |
| `jenkins retrigger <job> <number>` | Retrigger via Gerrit Trigger plugin |
| `jenkins describe [--command NAME]` | Machine-readable tool/command description (always JSON) |

### Matrix Builds

Jobs like `lustre-reviews` run a matrix of configurations (arch, distro, build type).
The `build` command shows all runs with per-config status. Use `run-console` to fetch
logs for a specific configuration — pass the config string exactly as shown in `build` output.

### Build Number Aliases

For `build` and `console`, `BUILD_NUMBER` is optional and accepts
special aliases in addition to numeric IDs:
- `lastBuild` — most recent build (the default when omitted)
- `lastFailedBuild` — most recent failed build
- `lastSuccessfulBuild` — most recent successful build

`run-console`, `abort`, and `retrigger` require an explicit numeric
build number (aliases are rejected).

## LLM Context Awareness

- Console output defaults to last 200 lines (`--tail 200`); use `--head N` for the start
- Use `--grep PATTERN` to filter console output by regex — returns only matching lines with line numbers
- `builds` defaults to last 10 builds; use `--limit N` for more

## License

MIT
