"""CLI: serve the dashboards, refresh once, or export a static page."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app import AppState, create_app, refresh_once
from .config import COMMUNITY_PROJECTS, Config
from .fetcher import GerritFetcher
from .store import user_store


def apply_project_args(cfg: Config, args: argparse.Namespace) -> None:
    """CLI overrides for the deployment config.

    --projects/--community override GD_PROJECTS/GD_COMMUNITY; --boards
    optionally narrows which usernames get a board (default: any).
    """
    if getattr(args, "projects", None):
        cfg.projects = [p.strip() for p in args.projects.split(",") if p.strip()]
    elif getattr(args, "community", False):
        cfg.projects = list(COMMUNITY_PROJECTS)
    if getattr(args, "boards", None):
        cfg.boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    if cfg.boards and cfg.default_user not in cfg.boards:
        cfg.boards.append(cfg.default_user)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gerrit-dashboard",
        description="Per-user Gerrit attention dashboards (read-only against Gerrit).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the web dashboard")
    p_serve.add_argument("-H", "--host", default=None)
    p_serve.add_argument("-p", "--port", type=int, default=None)
    p_serve.add_argument("--interval", type=int, default=None,
                         help="default auto-refresh interval seconds (0 = off)")

    p_refresh = sub.add_parser("refresh", help="fetch once and persist the snapshot")
    p_refresh.add_argument("--user", default=None, help="Gerrit username (default: GD_DEFAULT_USER)")

    p_snap = sub.add_parser("snapshot", help="export a static HTML page")
    p_snap.add_argument("-o", "--output", default="reports/dashboard.html")
    p_snap.add_argument("--user", default=None, help="Gerrit username (default: GD_DEFAULT_USER)")
    p_snap.add_argument("--cached", action="store_true",
                        help="render from the persisted snapshot without fetching")

    for p in (p_serve, p_refresh, p_snap):
        p.add_argument("--community", action="store_true",
                       help="restrict to the public projects "
                            f"({', '.join(COMMUNITY_PROJECTS)}) — same as GD_COMMUNITY=1")
        p.add_argument("--projects", default=None, metavar="P1,P2",
                       help="comma-separated project allowlist "
                            "(overrides GD_PROJECTS/GD_COMMUNITY)")
    for p in (p_serve, p_snap):
        p.add_argument("--boards", default=None, metavar="U1,U2",
                       help="only these usernames get a board (overrides "
                            "GD_BOARDS; default: any Gerrit user)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = Config.from_env()
    apply_project_args(cfg, args)
    if args.cmd == "serve":
        if args.host:
            cfg.host = args.host
        if args.port:
            cfg.port = args.port
        if args.interval is not None:
            cfg.refresh_seconds = args.interval
        app = create_app(cfg, start_refresher=True)
        try:
            app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)
        finally:
            # Unblock refreshers and cancel queued fetches so Ctrl-C
            # exits promptly instead of hanging on in-flight enrichment.
            # Snapshot the dict: straggler requests may still add users.
            for ctx in list(app.config["gd"]["users"].values()):
                ctx.teardown()
        return 0

    user = getattr(args, "user", None) or cfg.default_user
    if not user:
        print("no user: pass --user, or set GD_DEFAULT_USER / GERRIT_USER",
              file=sys.stderr)
        return 2
    store = user_store(cfg.data_dir, user, migrate_legacy=(user == cfg.default_user))

    if args.cmd == "refresh":
        fetcher = GerritFetcher(cfg, store, target=user)
        snapshot = refresh_once(fetcher, store, cfg, AppState())
        print(f"snapshot written: {store.data_dir / 'snapshot.json'} ({snapshot['kpis']})")
        return 0

    if args.cmd == "snapshot":
        app = create_app(cfg, start_refresher=False)
        if args.cached:
            snapshot = store.load_snapshot(projects=cfg.projects)
            if snapshot is None:
                print("no cached snapshot (or one built under a different "
                      "project filter); run without --cached first", file=sys.stderr)
                return 1
        else:
            fetcher = GerritFetcher(cfg, store, target=user)
            snapshot = refresh_once(fetcher, store, cfg, AppState())
        with app.test_request_context():
            from flask import render_template
            html = render_template(
                "dashboard.html",
                snapshot=snapshot,
                refreshing=False,
                error=None,
                form_error="",
                static_mode=True,
                gerrit_url=cfg.gerrit_base_url,
                user=user,
            )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
        print(f"wrote {out}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
