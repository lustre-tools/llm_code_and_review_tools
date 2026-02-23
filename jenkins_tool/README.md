# Jenkins Tool

A thin, LLM-agent-focused CLI for the Jenkins build server. Query build status,
console output, and Gerrit-triggered builds.

## Installation

```bash
pip install -e .
```

## Configuration

Set environment variables:

```bash
export JENKINS_URL="https://build.whamcloud.com"
export JENKINS_USER="your-username"
export JENKINS_TOKEN="your-api-token"
```

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

Commands default to human-readable tables and text. Add `--json` to any command
for the standard JSON envelope (`ok`, `data`, `meta`) for programmatic use:

```bash
jenkins builds lustre-reviews --limit 10 --json
jenkins build lustre-reviews 121881 --json
```

Error output goes to stderr in human mode, or to stdout as
`{"ok": false, "error": {...}}` in `--json` mode.

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

### Matrix Builds

Jobs like `lustre-reviews` run a matrix of configurations (arch, distro, build type).
The `build` command shows all runs with per-config status. Use `run-console` to fetch
logs for a specific configuration — pass the config string exactly as shown in `build` output.

### Build Number Aliases

`BUILD_NUMBER` accepts special aliases in addition to numeric IDs:
- `lastBuild` — most recent build (default)
- `lastFailedBuild` — most recent failed build
- `lastSuccessfulBuild` — most recent successful build

## LLM Context Awareness

- Console output defaults to last 200 lines (`--tail 200`); use `--head N` for the start
- Use `--grep PATTERN` to filter console output by regex — returns only matching lines with line numbers
- `builds` defaults to last 10 builds; use `--limit N` for more

## License

MIT
