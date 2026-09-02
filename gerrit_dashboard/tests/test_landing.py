"""The board index at `/`: which boards exist, how stale, and open/add."""

import json
import time

import pytest

from gerrit_dashboard import app as app_mod
from gerrit_dashboard.app import create_app
from gerrit_dashboard.config import Config
from gerrit_dashboard.store import SNAPSHOT_SCHEMA, list_boards, user_store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No live Gerrit: unknown tokens fail to resolve, seeded boards
    resolve through the on-disk fallback."""
    class DeadResolver:
        def __init__(self, *a, **k):
            self.rest = self
            self.username = "alex"

        def get(self, *a, **k):
            raise ConnectionError("offline test")

    monkeypatch.setattr(app_mod, "GerritCommentsClient", DeadResolver)


def seed(cfg, username, *, name=None, action=0, fetched_at=None, schema=SNAPSHOT_SCHEMA):
    store = user_store(cfg.data_dir, username)
    snap = {
        "schema": schema,
        "self": {"account_id": 1, "username": username, "name": name or username},
        "generated_at": "2026-08-19 09:00:00 UTC",
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
        "kpis": {"action": action, "mine_open": 3, "reviews": 2, "landing": 1},
        "branches": [{"name": "master", "count": 3}],
        "projects": [],
    }
    # Written directly: save_snapshot would also write the summary, and
    # some tests need the pre-summary layout.
    (store.data_dir / "snapshot.json").write_text(json.dumps(snap))
    return store


def client_for(tmp_path, **over):
    cfg = Config()
    cfg.data_dir = tmp_path
    for k, v in over.items():
        setattr(cfg, k, v)
    app = create_app(cfg, start_refresher=False)
    app.config.update(TESTING=True)
    return app.test_client(), cfg


class TestListBoards:
    def test_empty_data_dir(self, tmp_path):
        assert list_boards(tmp_path) == []

    def test_lists_and_sorts_newest_first(self, tmp_path):
        cfg = Config(); cfg.data_dir = tmp_path
        seed(cfg, "old", fetched_at=time.time() - 86400)
        seed(cfg, "fresh", fetched_at=time.time())
        assert [b["username"] for b in list_boards(tmp_path)] == ["fresh", "old"]

    def test_board_without_snapshot_still_listed(self, tmp_path):
        cfg = Config(); cfg.data_dir = tmp_path
        user_store(tmp_path, "brandnew")          # dir only, no snapshot
        boards = list_boards(tmp_path)
        assert [b["username"] for b in boards] == ["brandnew"]
        assert boards[0]["summary"] is None

    def test_outdated_schema_is_marked_not_dropped(self, tmp_path):
        cfg = Config(); cfg.data_dir = tmp_path
        seed(cfg, "ancient", name="Ancient User", schema=SNAPSHOT_SCHEMA - 1)
        board = list_boards(tmp_path)[0]
        assert board["summary"]["state"] == "outdated"
        assert board["summary"]["self"]["name"] == "Ancient User"

    def test_summary_is_cached_so_big_snapshots_parse_once(self, tmp_path):
        cfg = Config(); cfg.data_dir = tmp_path
        store = seed(cfg, "alex")
        assert not (store.data_dir / "summary.json").exists()
        list_boards(tmp_path)
        assert (store.data_dir / "summary.json").exists()
        # With the sidecar present the snapshot is not needed at all.
        (store.data_dir / "snapshot.json").unlink()
        assert list_boards(tmp_path)[0]["summary"]["state"] == "ok"

    def test_pre_state_summary_is_still_usable(self, tmp_path):
        # Summaries written before `state` existed must not read as broken.
        cfg = Config(); cfg.data_dir = tmp_path
        store = seed(cfg, "alex")
        (store.data_dir / "summary.json").write_text(json.dumps({
            "schema": SNAPSHOT_SCHEMA, "self": {"username": "alex"},
            "generated_at": "x", "fetched_at": 1, "kpis": {"action": 4}}))
        assert list_boards(tmp_path)[0]["summary"]["state"] == "ok"

    def test_names_that_are_not_valid_usernames_are_ignored(self, tmp_path):
        # Stray directories (or ones a hand-edit created) must not show up
        # as boards; "..hidden" is a normal name, "bad name" is not.
        (tmp_path / "users").mkdir()
        (tmp_path / "users" / "bad name").mkdir()
        (tmp_path / "users" / "ok1").mkdir()
        (tmp_path / "users" / "stray.txt").write_text("x")
        assert [b["username"] for b in list_boards(tmp_path)] == ["ok1"]


class TestLandingPage:
    def test_lists_boards_with_counts_and_age(self, tmp_path):
        client, cfg = client_for(tmp_path)
        seed(cfg, "alex", name="Alex Dev", action=7)
        html = client.get("/").get_data(as_text=True)
        assert "Alex Dev" in html and "alex" in html
        assert "7" in html and "needs action" in html
        assert "</strong> board on this instance" in html   # singular

    def test_empty_instance_invites_the_first_board(self, tmp_path):
        client, _ = client_for(tmp_path)
        html = client.get("/").get_data(as_text=True)
        assert "No boards yet" in html
        assert "Nothing indexed yet" in html

    def test_default_board_is_marked(self, tmp_path):
        client, cfg = client_for(tmp_path, default_user="alex")
        seed(cfg, "alex")
        assert "default" in client.get("/").get_data(as_text=True)

    def test_no_longer_redirects_to_the_default_board(self, tmp_path):
        client, cfg = client_for(tmp_path, default_user="alex")
        seed(cfg, "alex")
        assert client.get("/").status_code == 200


class TestOpenBoard:
    def test_known_board_redirects(self, tmp_path):
        client, cfg = client_for(tmp_path)
        seed(cfg, "alex")
        resp = client.get("/open?user=alex")
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/alex/")

    @pytest.mark.parametrize("token,msg", [
        ("", "enter a Gerrit username"),
        ("..", "not a valid Gerrit username"),
        ("has space", "not a valid Gerrit username"),
        ("ghost", "no Gerrit account"),
    ])
    def test_bad_input_returns_to_the_index_with_a_reason(self, tmp_path, token, msg):
        client, _ = client_for(tmp_path)
        resp = client.get(f"/open?user={token}")
        assert resp.status_code == 302
        follow = client.get(resp.headers["Location"]).get_data(as_text=True)
        assert msg in follow

    def test_leading_at_is_tolerated(self, tmp_path):
        client, cfg = client_for(tmp_path)
        seed(cfg, "alex")
        resp = client.get("/open?user=@alex")
        assert resp.headers["Location"].endswith("/alex/")

    def test_board_allowlist_is_enforced(self, tmp_path):
        client, cfg = client_for(tmp_path, boards=["alex"])
        seed(cfg, "alex")
        resp = client.get("/open?user=someone")
        follow = client.get(resp.headers["Location"]).get_data(as_text=True)
        assert "board list" in follow
