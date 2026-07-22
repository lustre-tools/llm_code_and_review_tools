# Confluence tool (`confluence`, alias `cf`)

Read-only Confluence CLI for LLM agents. Search and read wiki pages with
structured JSON output. Like the other tools in this repo, it prints the JSON
data payload to stdout by default; use `--envelope` for the full
`{ok, data, meta}` wrapper.

**Read-only by design.** There are no create/update/delete commands.

## Instances

Configured in `~/.confluence-tool.json` (multi-instance, same shape as
`~/.jira-tool.json`):

| Instance | Server | Auth | Notes |
|---|---|---|---|
| `cloud` (default) | `https://<org>.atlassian.net/wiki` | basic (email + Atlassian API token) | Confluence Cloud. Same API token as JIRA Cloud. |
| `whamcloud` | `https://wiki.whamcloud.com` | **none (anonymous)** | Public Lustre community wiki. |

**Why anonymous for whamcloud?** The whamcloud wiki is the public,
open-source Lustre community wiki. The tool sends **no credentials** to it and
exposes **no write commands**, so only public (open-source) Lustre articles are
ever reachable there — the guarantee is structural, not policy.

Select an instance with `-I`:

```bash
confluence search 'nodemap'                    # default instance ('cloud')
confluence -I whamcloud search 'lnet routing'  # public Lustre wiki
```

## Commands

```bash
confluence search <text> [--space KEY] [--cql] [--limit N]
confluence get <id-or-url> [--output text]
confluence spaces [--limit N]
confluence check                 # connectivity / auth for the selected instance
confluence config-sample         # print a sample ~/.confluence-tool.json
confluence describe              # machine-readable API surface
```

- `search` wraps plain text as `type = page AND text ~ "<query>"`. Pass `--cql`
  to supply a raw CQL expression instead, and `--space KEY` to scope it.
- `get` accepts a bare page id or a full Confluence page URL; the HTML body is
  rendered to plain text in the `text` field.

## Configuration

```bash
confluence config-sample > ~/.confluence-tool.json
# then edit in your org URL, email, and Atlassian API token for 'cloud'
```

Cloud API token: `id.atlassian.com/manage-profile/security/api-tokens`
(the same token type used by the JIRA tool's `cloud` instance).

`CONFLUENCE_SERVER` / `CONFLUENCE_TOKEN` env vars override the config when no
`-I` instance is selected.

## Development

```bash
cd confluence_tool && pip install -e .
pytest -m unit
```
