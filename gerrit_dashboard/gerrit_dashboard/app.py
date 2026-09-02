"""Flask app: serves per-user dashboards, one refresher per user.

URLs are /<user>/... where <user> is a Gerrit username (account ids
canonicalize via redirect).  All Gerrit fetching uses the configured
credentials — any authenticated account can read every user's changes;
stars are the only self-only data and simply stay empty on other
users' boards.  The bare / redirects to the default user.

Each user's page renders from that user's in-memory snapshot
(persisted under data/users/<user>/); a per-user refresher thread
updates it on its interval or on demand.  The registry is a bounded
LRU: least-recently-used contexts are torn down (thread stopped, pool
closed) so URL scanning cannot accumulate threads, and failed account
resolutions are negative-cached so unknown tokens don't hammer Gerrit.

Deploy note: refreshers live in the web process, so run a single
worker (flask dev server, or `gunicorn -w 1 'gerrit_dashboard.app:create_app()'`).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from gerrit_cli.client import GerritCommentsClient
from werkzeug.middleware.proxy_fix import ProxyFix

from .classify import build_snapshot
from .config import Config
from .fetcher import GerritFetcher
from .store import SAFE_USERNAME_RE, list_boards, user_store

log = logging.getLogger(__name__)

# Usernames go into Gerrit query strings and filesystem paths; the store
# enforces the same rule, so keep one definition.
USER_RE = SAFE_USERNAME_RE

# Seconds a failed account resolution stays cached (scanner tokens like
# "robots.txt" pass USER_RE but must not cost a Gerrit call per hit).
NEGATIVE_TTL = 300
# Hard cap on the resolver caches so a flood of distinct unknown tokens
# cannot grow process memory without bound (LRU-evicted beyond this).
RESOLVER_CACHE_MAX = 4096

# Lowest auto-refresh interval the UI offers and the API accepts.  A
# stored value below it (hand-edited, written by an older version, or
# set by someone else on a shared instance) is clamped when loaded
# rather than silently honored forever.
MIN_INTERVAL = 300


def _age_since(epoch: float) -> str:
    """Compact 'how old is this board' label for the index."""
    if not epoch:
        return ""
    secs = max(0, int(time.time() - epoch))
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: dict | None = None
        self.refreshing = False
        self.last_error: str | None = None
        self.last_duration: float | None = None
        self.wakeup = threading.Event()
        self.stop = threading.Event()
        # Auto-refresh interval in seconds; 0 = disabled (manual only).
        self.interval = 0
        # Live fetch progress: {phase, done, total} while refreshing.
        self.progress: dict | None = None
        # Serializes refreshes of THIS board: however many are requested,
        # only one ever runs (the rest coalesce into the next one).
        self.refresh_lock = threading.Lock()
        self.last_refresh_at = 0.0
        # A refresh has been asked for but has not started yet (waiting
        # out the rate cap) — the UI shows this as "queued".
        self.pending = False

    def request_refresh(self) -> None:
        with self.lock:
            self.pending = True
            self.wakeup.set()

    def take_wakeup(self) -> bool:
        """Atomically consume a pending wakeup (no lost-set window)."""
        with self.lock:
            pending = self.wakeup.is_set()
            self.wakeup.clear()
            return pending

    def cooldown_remaining(self, min_seconds: int) -> float:
        """Seconds until this board may refresh again (0 = now)."""
        if min_seconds <= 0 or not self.last_refresh_at:
            return 0.0
        return max(0.0, self.last_refresh_at + min_seconds - time.time())


class UserCtx:
    """Everything belonging to one user's board.  Constructed OUTSIDE the
    registry lock (does disk I/O); start() is called only once the ctx
    won the registry insert."""

    def __init__(self, cfg: Config, username: str) -> None:
        self.cfg = cfg
        self.username = username
        self.store = user_store(cfg.data_dir, username,
                                migrate_legacy=(username == cfg.default_user))
        self.fetcher = GerritFetcher(cfg, self.store, target=username)
        self.state = AppState()
        self.state.snapshot = self.store.load_snapshot(projects=cfg.projects)
        saved = self.store.load_settings().get("refresh_seconds")
        interval = saved if isinstance(saved, int) else cfg.refresh_seconds
        self.state.interval = MIN_INTERVAL if 0 < interval < MIN_INTERVAL else interval

    def start(self) -> None:
        threading.Thread(
            target=_refresher_loop, args=(self.fetcher, self.store, self.cfg, self.state),
            name=f"gd-refresher-{self.username}", daemon=True,
        ).start()

    def teardown(self) -> None:
        self.state.stop.set()
        self.state.request_refresh()  # unblock a wait(None)
        self.fetcher.close()


def refresh_once(fetcher: GerritFetcher, store, config: Config, state: AppState) -> dict:
    """Fetch + classify + persist one board.

    Only one refresh per board runs at a time: a caller arriving while
    one is in flight returns the current snapshot instead of starting a
    second full fetch against Gerrit.
    """
    if not state.refresh_lock.acquire(blocking=False):
        log.info("refresh already running; skipping duplicate")
        with state.lock:
            return state.snapshot or {}
    try:
        return _refresh_locked(fetcher, store, config, state)
    finally:
        state.last_refresh_at = time.time()
        state.refresh_lock.release()


def _refresh_locked(fetcher: GerritFetcher, store, config: Config, state: AppState) -> dict:
    with state.lock:
        state.refreshing = True
        state.pending = False
        state.progress = None
    started = time.time()

    def on_progress(phase: str, done: int, total: int) -> None:
        with state.lock:
            state.progress = {"phase": phase, "done": done, "total": total}

    try:
        bundle = fetcher.fetch_bundle(progress=on_progress)
        snapshot = build_snapshot(bundle, config)
        store.save_snapshot(snapshot)
        with state.lock:
            state.snapshot = snapshot
            state.last_error = "; ".join(bundle["errors"]) if bundle["errors"] else None
            state.last_duration = time.time() - started
        return snapshot
    except Exception as exc:  # noqa: BLE001 - keep serving the stale snapshot
        log.exception("refresh failed")
        with state.lock:
            state.last_error = str(exc)
            state.last_duration = time.time() - started
        raise
    finally:
        with state.lock:
            state.refreshing = False
            state.progress = None


def _refresher_loop(fetcher: GerritFetcher, store, config: Config, state: AppState) -> None:
    first = True
    while not state.stop.is_set():
        manual = state.take_wakeup()
        # With auto-refresh disabled (interval 0) only refresh on demand —
        # plus once at startup when there is no snapshot at all.
        if state.interval > 0 or manual or (first and state.snapshot is None):
            # Rate cap: hold off until this board's cooldown expires, so
            # repeated requests cost at most one refresh per window
            # instead of hammering Gerrit.  The request is deferred, not
            # dropped — the refresh still happens, just a bit later.
            wait = state.cooldown_remaining(config.min_refresh_seconds)
            if wait > 0 and state.stop.wait(timeout=wait):
                break
            try:
                refresh_once(fetcher, store, config, state)
            except Exception:  # noqa: BLE001
                pass  # already logged; stale snapshot keeps serving
        first = False
        state.wakeup.wait(timeout=state.interval if state.interval > 0 else None)


def create_app(config: Config | None = None, start_refresher: bool = True) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__)
    # Honor X-Forwarded-{Proto,Host,Prefix} from the fronting nginx so
    # url_for() builds /gerrit/... URLs behind https://host/gerrit/.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    users: OrderedDict[str, UserCtx] = OrderedDict()
    accounts: "OrderedDict[str, dict]" = OrderedDict()   # url token -> resolved account
    negative: "OrderedDict[str, float]" = OrderedDict()  # url token -> retry-after ts
    resolver_lock = threading.Lock()    # guards accounts/negative (worker threads race)
    registry_lock = threading.Lock()
    resolver_holder: list[GerritCommentsClient] = []
    app.config["gd"] = {"config": cfg, "users": users, "registry_lock": registry_lock}

    @app.context_processor
    def _inject_chrome() -> dict:
        # Deployment web chrome available to every template: the first-visit
        # theme default and the optional "back to the parent site" link.
        return {
            "default_theme": cfg.default_theme,
            "site_name": cfg.site_name,
            "site_home": cfg.site_home or "/",
        }

    def _resolver() -> GerritCommentsClient:
        # Lazy: `snapshot --cached` must work without Gerrit credentials.
        if not resolver_holder:
            resolver_holder.append(GerritCommentsClient())
        return resolver_holder[0]

    def _cache_put(cache: "OrderedDict", key: str, value) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > RESOLVER_CACHE_MAX:
            cache.popitem(last=False)  # drop least-recently-used

    def canonical_user(token: str) -> str | None:
        """Resolve a username or account id to the canonical url token."""
        if not USER_RE.match(token):
            return None
        # Board allowlist: on a restricted (public) instance only the
        # listed usernames resolve, and everything else is rejected
        # WITHOUT a Gerrit lookup — no account-enumeration oracle, no
        # arbitrary token spawning a privileged fetch or an on-disk dir.
        if cfg.boards and token not in cfg.boards:
            return None
        with resolver_lock:
            acct = accounts.get(token)
            if acct is not None:
                accounts.move_to_end(token)
            else:
                retry_at = negative.get(token, 0)
                if time.time() < retry_at:
                    return None
        if acct is None:
            try:
                acct = _resolver().rest.get(f"/accounts/{token}")
            except Exception:  # noqa: BLE001 - unknown account or Gerrit away
                # Boards that exist on disk keep working through Gerrit
                # outages/restarts; genuinely unknown tokens are
                # negative-cached so scanners cost one call per TTL.
                if (cfg.data_dir / "users" / token / "snapshot.json").is_file():
                    return token
                with resolver_lock:
                    _cache_put(negative, token, time.time() + NEGATIVE_TTL)
                return None
            with resolver_lock:
                _cache_put(accounts, token, acct)
                username = acct.get("username") or ""
                if username and username not in accounts:
                    _cache_put(accounts, username, acct)
        username = acct.get("username") or ""
        # A username outside our charset cannot be a URL token — fall
        # back to the numeric id as the canonical form.
        if username and USER_RE.match(username):
            return username
        return str(acct.get("_account_id"))

    def get_ctx(user: str) -> UserCtx:
        with registry_lock:
            ctx = users.get(user)
            if ctx is not None:
                users.move_to_end(user)
                return ctx
        # Construct outside the lock (disk I/O, legacy migration) —
        # double-checked insert; the loser discards its context.
        fresh = UserCtx(cfg, user)
        evicted: list[UserCtx] = []
        with registry_lock:
            ctx = users.get(user)
            if ctx is None:
                users[user] = fresh
                ctx = fresh
                fresh = None
                while len(users) > cfg.max_users:
                    for k in users:  # oldest first
                        if k != user and k != cfg.default_user:
                            evicted.append(users.pop(k))
                            break
                    else:
                        break
        if fresh is not None:
            fresh.teardown()
        elif start_refresher:
            ctx.start()
        for old in evicted:
            log.info("evicting idle user context %s", old.username)
            old.teardown()
        return ctx

    def resolve_or_404(user: str):
        """Returns (ctx, None) or (None, redirect-to-canonical)."""
        canonical = canonical_user(user)
        if canonical is None:
            abort(404, "unknown Gerrit user")
        if canonical != user:
            view = request.endpoint or "index"
            args = dict(request.view_args or {})
            args["user"] = canonical
            qargs = {k: v for k, v in request.args.to_dict(flat=False).items()
                     if k not in args}
            return None, redirect(url_for(view, **args, **qargs))
        return get_ctx(canonical), None

    def _foreign_origin() -> bool:
        # Light CSRF guard for an internal tool: state-changing requests
        # from a browser carry Origin; reject ones from other sites.
        origin = request.headers.get("Origin")
        return bool(origin) and origin.rstrip("/") != request.host_url.rstrip("/")

    def _reject_write():
        """403 for a mutating request from another site.

        Note these endpoints only touch this app's own per-user files
        (watchlist/hidden/interval) and never write to Gerrit.  Without
        auth in front, any client can edit any board — deliberate for a
        shared instance; put basic-auth in front for a closed one.
        """
        if _foreign_origin():
            return jsonify({"error": "cross-origin request rejected"}), 403
        return None

    @app.route("/")
    def root():
        """Board index: which boards exist, how fresh, and open/add one."""
        boards = list_boards(cfg.data_dir)
        live = set()
        with registry_lock:
            live = set(users)
        for b in boards:
            b["live"] = b["username"] in live
            b["age"] = _age_since((b["summary"] or {}).get("fetched_at", 0))
        return render_template(
            "landing.html",
            boards=boards,
            default_user=cfg.default_user,
            gerrit_url=cfg.gerrit_base_url,
            error=request.args.get("err", ""),
            added=request.args.get("added", ""),
        )

    @app.route("/open")
    def open_board():
        """Landing-page form target: /open?user=x → /x/ (or back with why)."""
        token = (request.args.get("user") or "").strip().lstrip("@")
        if not token:
            return redirect(url_for("root", err="enter a Gerrit username or account id"))
        if not USER_RE.match(token):
            return redirect(url_for("root", err=f"{token!r} is not a valid Gerrit username"))
        if cfg.boards and token not in cfg.boards:
            return redirect(url_for("root", err=f"{token} is not on this instance's board list"))
        if canonical_user(token) is None:
            return redirect(url_for("root", err=f"no Gerrit account for {token!r}"))
        return redirect(url_for("index", user=token))

    @app.route("/<user>/")
    def index(user: str):
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        state = ctx.state
        with state.lock:
            snapshot = state.snapshot
            refreshing = state.refreshing
            error = state.last_error
        return render_template(
            "dashboard.html",
            snapshot=snapshot,
            refreshing=refreshing,
            error=error,
            form_error=request.args.get("werr", ""),
            static_mode=False,
            gerrit_url=cfg.gerrit_base_url,
            user=user,
        )

    @app.route("/<user>/api/data")
    def api_data(user: str):
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        with ctx.state.lock:
            return jsonify(ctx.state.snapshot or {})

    @app.route("/<user>/api/status")
    def api_status(user: str):
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        state = ctx.state
        queued_for = state.cooldown_remaining(cfg.min_refresh_seconds)
        with state.lock:
            return jsonify({
                "refreshing": state.refreshing,
                "pending": state.pending,
                "queued_for": round(queued_for, 1),
                "progress": state.progress,
                "error": state.last_error,
                "duration": state.last_duration,
                "interval": state.interval,
                "snapshot_id": (state.snapshot or {}).get("snapshot_id"),
                "generated_at": (state.snapshot or {}).get("generated_at"),
            })

    @app.route("/<user>/api/refresh", methods=["POST"])
    def api_refresh(user: str):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        ctx.state.request_refresh()
        return jsonify({"ok": True}), 202

    @app.route("/<user>/api/interval", methods=["POST"])
    def api_interval(user: str):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = request.form
        try:
            seconds = int(payload.get("seconds"))
        except (TypeError, ValueError):
            return jsonify({"error": "seconds must be an integer"}), 400
        # Floor matches the lowest option the UI offers; anything shorter
        # is pure load on Gerrit for a board nobody is watching live.
        if seconds != 0 and not MIN_INTERVAL <= seconds <= 86400:
            return jsonify(
                {"error": f"seconds must be 0 (off) or {MIN_INTERVAL}..86400"}), 400
        ctx.state.interval = seconds
        ctx.store.update_settings(refresh_seconds=seconds)
        ctx.state.request_refresh()  # re-arm the loop with the new timeout
        return jsonify({"ok": True, "interval": seconds})

    @app.route("/<user>/api/hidden", methods=["POST"])
    def hidden_add(user: str):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = request.form
        change = (payload.get("change") or "").strip()
        if not change:
            return jsonify({"error": "missing change"}), 400
        try:
            number = ctx.store.hidden_add(change, limit=cfg.max_hidden)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        ctx.state.request_refresh()
        return jsonify({"ok": True, "number": number}), 201

    @app.route("/<user>/api/hidden/<int:number>", methods=["DELETE"])
    def hidden_remove(user: str, number: int):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        if not ctx.store.hidden_remove(number):
            return jsonify({"error": "not hidden"}), 404
        ctx.state.request_refresh()
        return jsonify({"ok": True})

    @app.route("/<user>/api/watchlist", methods=["POST"])
    def watchlist_add(user: str):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = request.form
        change = (payload.get("change") or "").strip()
        note = (payload.get("note") or "").strip()
        if not change:
            return jsonify({"error": "missing change"}), 400
        try:
            entry = ctx.store.watchlist_add(change, note, limit=cfg.max_watchlist)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        ctx.state.request_refresh()
        return jsonify(entry), 201

    @app.route("/<user>/api/watchlist/<int:number>", methods=["DELETE"])
    def watchlist_remove(user: str, number: int):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        if not ctx.store.watchlist_remove(number):
            return jsonify({"error": "not on watchlist"}), 404
        ctx.state.request_refresh()
        return jsonify({"ok": True})

    # Convenience for form-posts without JS fetch (progressive enhancement).
    @app.route("/<user>/watchlist/add", methods=["POST"])
    def watchlist_add_form(user: str):
        blocked = _reject_write()
        if blocked:
            return blocked
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        change = (request.form.get("change") or "").strip()
        note = (request.form.get("note") or "").strip()
        try:
            ctx.store.watchlist_add(change, note, limit=cfg.max_watchlist)
            ctx.state.request_refresh()
        except ValueError as exc:
            return ("", 303, {"Location": url_for("index", user=user, werr=str(exc))})
        return ("", 303, {"Location": url_for("index", user=user)})

    @app.route("/<user>/export")
    def export(user: str):
        ctx, resp = resolve_or_404(user)
        if resp:
            return resp
        with ctx.state.lock:
            snapshot = ctx.state.snapshot
        if snapshot is None:
            return jsonify({"error": "no snapshot yet"}), 503
        html = render_template(
            "dashboard.html", snapshot=snapshot, refreshing=False,
            error=None, form_error="", static_mode=True,
            gerrit_url=cfg.gerrit_base_url, user=user,
        )
        return html, 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": "attachment; filename=dashboard.html",
        }

    return app
