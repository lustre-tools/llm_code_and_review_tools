"""Abuse resistance for a shared, unauthenticated deployment.

The dashboard never writes to Gerrit, and any user may open their own
board by username, so the guarantees that matter are about LOAD and
DISCLOSURE, not about blocking the features:
  * one refresh per board at a time, rate-capped;
  * the per-user lists that cost Gerrit fetches stay bounded;
  * "not available here" says the same thing whether a change does not
    exist or lives in a project this instance does not serve.
Plus the universal hardening from the pre-open-source audit.
"""

import argparse
import threading
import time

import pytest

from gerrit_dashboard import app as app_mod
from gerrit_dashboard import cli
from gerrit_dashboard.app import USER_RE, AppState, create_app, refresh_once
from gerrit_dashboard.config import Config
from gerrit_dashboard.store import user_store


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Account resolution fails fast (no live Gerrit); canonical_user
    then falls back to the on-disk snapshot for boards we pre-seed."""
    class DeadResolver:
        def __init__(self, *a, **k):
            self.rest = self
            self.username = "mvef"

        def get(self, *a, **k):
            raise ConnectionError("offline test")

    monkeypatch.setattr(app_mod, "GerritCommentsClient", DeadResolver)


def _seed_board(cfg, username):
    store = user_store(cfg.data_dir, username)
    (store.data_dir / "snapshot.json").write_text("{}")


def _client(tmp_path, seed="mvef", **over):
    cfg = Config()
    cfg.data_dir = tmp_path
    for k, v in over.items():
        setattr(cfg, k, v)
    if seed:
        _seed_board(cfg, seed)
    app = create_app(cfg, start_refresher=False)
    app.config.update(TESTING=True)
    return app.test_client(), cfg


# ---- USER_RE: path-navigation tokens can never become a store dir ----

class TestUserRe:
    @pytest.mark.parametrize("bad", [".", "..", "...", "._-", "", "a/b", "a b", "x" * 65])
    def test_rejects(self, bad):
        assert not USER_RE.match(bad)

    @pytest.mark.parametrize("ok", ["mvef", "1055", "a.b", "user-1", "a_b", "9"])
    def test_accepts(self, ok):
        assert USER_RE.match(ok)

    @pytest.mark.parametrize("bad", ["..", ".", ""])
    def test_store_refuses_path_navigating_names(self, tmp_path, bad):
        # Without this guard "" resolves to <data>/users and ".." to the
        # data root — and with migrate_legacy that STRANDS the legacy
        # files in a directory no board ever reads.
        with pytest.raises(ValueError, match="invalid username"):
            user_store(tmp_path, bad, migrate_legacy=True)

    def test_traversal_token_404s(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.get("/../").status_code == 404


# ---- the features stay usable without auth ----

class TestWritesStayAvailable:
    def test_refresh_accepted(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/mvef/api/refresh").status_code == 202

    def test_watchlist_and_hide_accepted(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/mvef/api/watchlist", json={"change": "12345"}).status_code == 201
        assert client.post("/mvef/api/hidden", json={"change": "12345"}).status_code == 201

    def test_cross_site_post_still_rejected(self, tmp_path):
        client, _ = _client(tmp_path)
        resp = client.post("/mvef/api/refresh", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403


# ---- one refresh per board at a time ----

class TestRefreshSerialization:
    def _fetcher(self, started, release):
        class SlowFetcher:
            def fetch_bundle(self_, progress=None):
                started.append(1)
                release.wait(timeout=5)
                return {"self": {"account_id": 1, "username": "u", "name": "U"},
                        "changes": {}, "roles": {}, "starred": set(), "watchlist": [],
                        "hidden": set(), "threads": {}, "next_queues": {},
                        "errors": [], "fetched_at": time.time()}
        return SlowFetcher()

    def test_concurrent_refresh_does_not_double_fetch(self, tmp_path):
        cfg = Config()
        cfg.data_dir = tmp_path
        store = user_store(tmp_path, "u")
        state = AppState()
        started: list[int] = []
        release = threading.Event()
        fetcher = self._fetcher(started, release)

        t = threading.Thread(target=refresh_once, args=(fetcher, store, cfg, state))
        t.start()
        while not started:
            time.sleep(0.01)
        # Second caller arrives mid-flight: must NOT start another fetch.
        refresh_once(fetcher, store, cfg, state)
        assert started == [1]
        release.set()
        t.join(timeout=5)
        assert started == [1]

    def test_cooldown_defers_next_refresh(self):
        state = AppState()
        assert state.cooldown_remaining(30) == 0.0  # never refreshed yet
        state.last_refresh_at = time.time()
        assert 0 < state.cooldown_remaining(30) <= 30
        assert state.cooldown_remaining(0) == 0.0   # cap disabled

    def test_cooldown_elapsed(self):
        state = AppState()
        state.last_refresh_at = time.time() - 60
        assert state.cooldown_remaining(30) == 0.0

    def test_queued_refresh_is_visible(self, tmp_path):
        # A deferred refresh must surface as "pending" — otherwise the
        # button looks dead while the rate cap holds it back.
        client, _ = _client(tmp_path)
        client.post("/mvef/api/refresh")
        body = client.get("/mvef/api/status").get_json()
        assert body["pending"] or body["refreshing"]
        assert "queued_for" in body


# ---- bounded per-user lists (each watchlist entry = a fetch/refresh) ----

class TestListCaps:
    def test_watchlist_cap(self, tmp_path):
        store = user_store(tmp_path, "u")
        for n in range(5):
            store.watchlist_add(str(1000 + n), limit=5)
        with pytest.raises(ValueError, match="full"):
            store.watchlist_add("2000", limit=5)

    def test_hidden_cap(self, tmp_path):
        store = user_store(tmp_path, "u")
        for n in range(3):
            store.hidden_add(str(1000 + n), limit=3)
        with pytest.raises(ValueError, match="full"):
            store.hidden_add("2000", limit=3)

    def test_cap_returns_400(self, tmp_path):
        client, _ = _client(tmp_path, max_watchlist=1)
        assert client.post("/mvef/api/watchlist", json={"change": "1"}).status_code == 201
        resp = client.post("/mvef/api/watchlist", json={"change": "2"})
        assert resp.status_code == 400
        assert b"full" in resp.data

    def test_note_is_truncated(self, tmp_path):
        store = user_store(tmp_path, "u")
        entry = store.watchlist_add("1", "x" * 5000)
        assert len(entry["note"]) == 200

    def test_interval_floor(self, tmp_path):
        client, _ = _client(tmp_path)
        assert client.post("/mvef/api/interval", json={"seconds": 60}).status_code == 400
        assert client.post("/mvef/api/interval", json={"seconds": 300}).status_code == 200
        assert client.post("/mvef/api/interval", json={"seconds": 0}).status_code == 200

    def test_stored_sub_floor_interval_is_clamped(self, tmp_path):
        # A too-short interval already on disk must not keep hammering
        # Gerrit just because it predates the floor.
        from gerrit_dashboard.app import MIN_INTERVAL, UserCtx
        cfg = Config()
        cfg.data_dir = tmp_path
        store = user_store(tmp_path, "u")
        store.update_settings(refresh_seconds=60)
        assert UserCtx(cfg, "u").state.interval == MIN_INTERVAL
        store.update_settings(refresh_seconds=0)
        assert UserCtx(cfg, "u").state.interval == 0   # off stays off


# ---- optional board allowlist (default: anyone) ----

class TestBoardAllowlist:
    def test_default_allows_any_username(self, tmp_path):
        cfg = Config()
        assert cfg.boards == []          # anyone, by design
        client, _ = _client(tmp_path, seed="someone")
        assert client.get("/someone/").status_code == 200

    def test_allowlist_rejects_before_any_gerrit_lookup(self, tmp_path, monkeypatch):
        client, _ = _client(tmp_path, boards=["mvef"])
        constructed: list[int] = []
        real_init = app_mod.GerritCommentsClient.__init__
        monkeypatch.setattr(
            app_mod.GerritCommentsClient, "__init__",
            lambda self, *a, **k: (constructed.append(1), real_init(self, *a, **k))[1])
        assert client.get("/999/").status_code == 404
        assert constructed == []

    def test_default_user_auto_added(self):
        cfg = Config()
        cfg.default_user = "mvef"
        cli.apply_project_args(
            cfg, argparse.Namespace(boards="alice,bob", projects=None, community=False))
        assert "mvef" in cfg.boards


class TestPostureConfig:
    def test_env_bounds(self, monkeypatch):
        monkeypatch.setenv("GD_MIN_REFRESH_SECONDS", "45")
        monkeypatch.setenv("GD_MAX_WATCHLIST", "7")
        monkeypatch.setenv("GD_BOARDS", "alice,bob")
        cfg = Config.from_env()
        assert cfg.min_refresh_seconds == 45
        assert cfg.max_watchlist == 7
        assert set(cfg.boards) >= {"alice", "bob"}
        assert cfg.default_user in cfg.boards
