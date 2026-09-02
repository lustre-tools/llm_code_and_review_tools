"""Store: watchlist roundtrip and change-ref parsing."""

import pytest

from gerrit_dashboard.store import Store, parse_change_ref


class TestParseChangeRef:
    def test_bare_number(self):
        assert parse_change_ref("67221") == 67221

    def test_full_url(self):
        assert parse_change_ref("https://review.whamcloud.com/c/fs/lustre-release/+/67221") == 67221

    def test_url_with_patchset(self):
        assert parse_change_ref("https://review.whamcloud.com/c/fs/lustre-release/+/67221/2") == 67221

    def test_short_url(self):
        assert parse_change_ref("https://review.whamcloud.com/67221") == 67221

    def test_trailing_slash_and_query(self):
        assert parse_change_ref("https://review.whamcloud.com/c/private/example-release/+/67153/?tab=comments") == 67153

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_change_ref("not-a-change")


class TestWatchlist:
    def test_add_remove_roundtrip(self, tmp_path):
        store = Store(tmp_path)
        entry = store.watchlist_add("https://review.whamcloud.com/c/fs/lustre-release/+/67221", "note x")
        assert entry["number"] == 67221
        assert store.load_watchlist()[0]["note"] == "note x"
        with pytest.raises(ValueError):
            store.watchlist_add("67221")
        assert store.watchlist_remove(67221)
        assert store.load_watchlist() == []
        assert not store.watchlist_remove(67221)

    def test_snapshot_roundtrip(self, tmp_path):
        from gerrit_dashboard.store import SNAPSHOT_SCHEMA
        store = Store(tmp_path)
        assert store.load_snapshot() is None
        store.save_snapshot({"schema": SNAPSHOT_SCHEMA, "kpis": {"action": 1}})
        assert store.load_snapshot() == {"schema": SNAPSHOT_SCHEMA, "kpis": {"action": 1}}

    def test_corrupt_files_tolerated(self, tmp_path):
        store = Store(tmp_path)
        (tmp_path / "snapshot.json").write_text("{broken")
        (tmp_path / "watchlist.json").write_text("{broken")
        assert store.load_snapshot() is None
        assert store.load_watchlist() == []


class TestSchemaGuard:
    def test_old_schema_snapshot_rejected(self, tmp_path):
        import json
        from gerrit_dashboard.store import SNAPSHOT_SCHEMA
        store = Store(tmp_path)
        (tmp_path / "snapshot.json").write_text(json.dumps({"schema": SNAPSHOT_SCHEMA - 1, "kpis": {}}))
        assert store.load_snapshot() is None
        (tmp_path / "snapshot.json").write_text(json.dumps({"kpis": {}}))  # no schema at all
        assert store.load_snapshot() is None

    def test_malformed_watchlist_entries_filtered(self, tmp_path):
        import json
        store = Store(tmp_path)
        (tmp_path / "watchlist.json").write_text(json.dumps(
            [{"number": 1}, {"note": "no number"}, "junk", {"number": "67221"}]))
        assert store.load_watchlist() == [{"number": 1}]


class TestSettings:
    def test_roundtrip_and_default(self, tmp_path):
        store = Store(tmp_path)
        assert store.load_settings() == {}
        store.save_settings({"refresh_seconds": 0})
        assert store.load_settings() == {"refresh_seconds": 0}


class TestUserStore:
    def test_per_user_dirs_and_migration(self, tmp_path):
        import json
        from gerrit_dashboard.store import SNAPSHOT_SCHEMA, user_store
        # legacy single-user layout
        (tmp_path / "watchlist.json").write_text(json.dumps([{"number": 1, "note": ""}]))
        (tmp_path / "snapshot.json").write_text(json.dumps({"schema": SNAPSHOT_SCHEMA, "kpis": {}}))
        s_default = user_store(tmp_path, "mvef", migrate_legacy=True)
        assert s_default.load_watchlist() == [{"number": 1, "note": ""}]
        assert not (tmp_path / "watchlist.json").exists()  # moved, not copied
        # another user starts empty and isolated
        s_other = user_store(tmp_path, "adilger")
        assert s_other.load_watchlist() == []
        s_other.watchlist_add("42", "theirs")
        assert s_default.load_watchlist() == [{"number": 1, "note": ""}]
        assert (tmp_path / "users" / "adilger" / "watchlist.json").exists()


class TestSettingsUpdate:
    def test_update_settings_rmw(self, tmp_path):
        store = Store(tmp_path)
        store.save_settings({"a": 1})
        out = store.update_settings(refresh_seconds=0)
        assert out == {"a": 1, "refresh_seconds": 0}
        assert store.load_settings() == {"a": 1, "refresh_seconds": 0}


class TestHidden:
    def test_hidden_roundtrip(self, tmp_path):
        store = Store(tmp_path)
        assert store.load_hidden() == []
        assert store.hidden_add("67221") == 67221
        store.hidden_add("https://review.whamcloud.com/c/fs/lustre-release/+/12345")
        store.hidden_add("67221")  # idempotent
        assert store.load_hidden() == [67221, 12345]
        assert store.hidden_remove(67221)
        assert not store.hidden_remove(67221)
        assert store.load_hidden() == [12345]
