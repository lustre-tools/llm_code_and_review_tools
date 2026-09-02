# gerrit-dashboard

A "what needs my attention?" dashboard for a Gerrit server running the
Lustre CI (review.whamcloud.com by default). It replaces the stock Gerrit
dashboard as a daily overview: an attention-ranked triage list, a watchlist
with CI state, and per-role sections (my patches / patches I carry for
others / reviews I owe). **Strictly read-only against Gerrit** — the tool
only ever issues GETs, and never votes, comments or stars.

Why it exists: Gerrit tells you what changed, not what you should do next.
This reads the CI bots' comments (jenkins builds, Maloo test sessions, the
gerrit janitor, checkpatch) and the review labels, then ranks everything by
what is actually blocking you.

## Quick start

The repository's `install.sh` installs the dashboard along with the
other tools (the sibling `gerrit_cli` package is the only non-PyPI
dependency — never `pip install gerrit-cli` from PyPI, an unrelated
project owns that name there):

```bash
./install.sh                              # venv + all tools, dashboard included
export GERRIT_URL=https://review.whamcloud.com
export GERRIT_USER=<your-gerrit-username>
export GERRIT_PASS=<HTTP password from Gerrit ▸ Settings ▸ HTTP Credentials>
gerrit-dashboard serve                    # http://127.0.0.1:1055
```

Open `/<gerrit-username>/` for a board; the bare `/` redirects to
`GD_DEFAULT_USER` (defaults to the credential's own account). Account ids
work too and redirect to the canonical username. A board is created lazily
on first visit, with its own watchlist, hidden list, settings and snapshot
under `data/users/<user>/`.

All fetching uses the configured credentials, so any account's board can be
viewed; Gerrit stars are self-only and appear only on the credential
owner's board. **There is no authentication** — anyone who can reach the
page can view any board and edit its watchlist. Put basic-auth in front of
it if that matters for your deployment.

Auto-refresh is off by default (zero Gerrit traffic until you press
Refresh). Refreshes of a board are serialised and rate-capped, so holding
the button down cannot amplify into Gerrit load.

Other commands:

```bash
gerrit-dashboard refresh [--user X]                  # one-shot fetch, no server
gerrit-dashboard snapshot -o out.html [--user X]     # static HTML export
gerrit-dashboard snapshot --cached -o out.html       # re-render without fetching
```

## What the sections mean

- **Needs your action** — everything P0/P1, deduped across roles, one
  why-line each. Signals older than a week collapse into "longstanding" so
  fresh problems stay visible. P0: build failed, enforced tests failed,
  needs rebase (checkpatch "cannot be cherry-picked"), janitor "failures
  unique to this patch". P1: unresolved review threads where the last word
  isn't yours, a human -1 on your patch, a new patchset that wiped out your
  -1 on someone else's.
- **Watchlist** — a local list (add a change number or URL, plus a note)
  showing build/test state, failed enforced tests with Maloo session links,
  and retest status. Replaces keeping browser tabs open; merged entries say
  so, so you can prune them.
- **My patches** — grouped: Failed / Feedback / In CI / Needs reviewers /
  Waiting on reviewers / Landing queue / Parked (WIP or your own -1).
- **Carrying** — open changes owned by others where you are the current
  uploader, committer or author (you rebased or adopted them), so their CI
  is effectively yours to watch.
- **Reviews** — Re-review needed (new patchset since your vote; a lost
  **-1** ranks first, since Gerrit silently drops outdated votes), Awaiting
  your review (CI-green first), not-green-yet, and already-voted.

Status slots per row: `B` build (jenkins vote + message), `J` janitor
initial testing, `T` tests (the Maloo vote is the aggregate; per-session
detail is in the expanded row), `R n/m` non-owner +1/+2 count against the
review threshold. Gerrit's attention set is deliberately ignored — it only
works if everyone maintains it.

## Configuration (environment, all optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `GD_HOST` / `GD_PORT` | 127.0.0.1 / 1055 | bind address |
| `GD_DATA_DIR` | `<pkg>/data` | snapshots, caches, watchlists |
| `GD_DEFAULT_USER` | `GERRIT_USER` | board the bare `/` redirects to |
| `GD_REFRESH_SECONDS` | 0 | auto-refresh interval; 0 = manual only |
| `GD_MIN_REFRESH_SECONDS` | 30 | floor between two refreshes of one board |
| `GD_MAX_WATCHLIST` / `GD_MAX_HIDDEN` | 200 / 2000 | per-board list caps |
| `GD_MAX_USERS` | 20 | live boards kept in memory (LRU) |
| `GD_BOARDS` | *(any user)* | restrict which usernames get a board |
| `GD_PROJECTS` | *(all visible)* | project allowlist |
| `GD_COMMUNITY` | 0 | shorthand for `GD_PROJECTS=fs/lustre-release` |
| `GD_NEXT_QUEUES` | `fs/lustre-release:master` | `project:branch` pairs whose `<branch>-next` staging branch marks a change as queued to land |
| `GD_VERIFIED_OVERRIDE` | *(none)* | emails whose Verified +1 stands in for a missing test-bot vote |
| `GD_BRANCH_COLORS` | *(none)* | `branch:token` tinting, e.g. `b2_15:tag,b2_16:run` (tokens: tag, run, ok, bad, warn, muted) |
| `GD_REVIEW_THRESHOLD` | 2 | distinct non-owner +1s a change needs |
| `GD_BACKPORT_REVIEW_THRESHOLD` | 1 | same, for backports (`Lustre-change:` trailer) |
| `GD_ACTION_RECENT_DAYS` | 7 | P0/P1 older than this → "longstanding" |
| `GD_NUDGE_DAYS` | 3 | CI-green + unreviewed age before "nudge reviewers" |
| `GD_STALE_DAYS` | 100 | "stalled" badge threshold |

Gerrit credentials come from the gerrit-cli environment layering
(`GERRIT_URL` / `GERRIT_USER` / `GERRIT_PASS`, e.g. via a shell profile or
`~/.config/gerrit-cli/.env`).

### Restricting to public projects

If the credentials can read projects the instance should not serve, set
`GD_PROJECTS` (or `GD_COMMUNITY=1`). The allowlist is applied to the Gerrit
**queries**, not just to the rendering, so restricted changes are never
fetched; anything that still arrives by another path (a watchlist entry) is
dropped before enrichment and unwatched, with a message that does not
distinguish "not allowed here" from "does not exist". Give such an instance
its own `GD_DATA_DIR`.

## Deployment

`create_app()` is a plain Flask factory:

```bash
gunicorn -w 1 -b 127.0.0.1:1055 'gerrit_dashboard.app:create_app()'
```

Use **one worker** — the background refresher lives in the web process.
Behind a reverse proxy the app honours `X-Forwarded-Proto/Host/Prefix`, so
it can be mounted under a path prefix. Alternatively run
`gerrit-dashboard refresh` from cron and serve `snapshot` exports as static
files.

## Data files (under `GD_DATA_DIR`, gitignored)

Per board, in `users/<username>/`:

- `snapshot.json` — last rendered state; the page serves instantly from it
  on start and it is stamped with the project allowlist it was built under.
- `enrich_cache.json` — per-change comment threads keyed by `meta_rev_id`,
  so an unchanged change costs no requests.
- `watchlist.json`, `hidden.json`, `settings.json` — your lists and
  preferences; safe to edit by hand.

## Development

```bash
python -m pytest tests/ -q     # offline, no credentials needed
```

Fixtures use verbatim bot-message shapes (jenkins build results, Maloo
enforced/optional verdicts, Autotest retests, checkpatch rebase refusals,
janitor unique-failure blocks) — the parsers are grammar-sensitive, so the
tests are the specification of what those messages look like. Dates in
fixtures are relative: the classifier has freshness boundaries, and
absolute dates rot.

Layout: `fetcher.py` (bulk queries + cached `/comments` enrichment),
`ci_parse.py` (bot-message parsers), `review_rules.py` (Code-Review gate),
`classify.py` (attention rules → snapshot), `app.py` (Flask + per-board
refreshers), `store.py` (per-user JSON files), `templates/dashboard.html`
(one self-contained page, no CDN).

Bumping `SNAPSHOT_SCHEMA` in `store.py` is required whenever the snapshot
layout changes; stale snapshots are then discarded instead of breaking the
template.
