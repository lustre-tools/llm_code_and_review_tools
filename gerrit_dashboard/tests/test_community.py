"""Restricted (community) mode.

Nothing outside the project allowlist may reach a bundle, snapshot or
persisted file — the guarantee a public deployment relies on when the
credentials can read more projects than the instance serves."""

import argparse
from types import SimpleNamespace

from conftest import ME, mk_bundle, mk_change

from gerrit_dashboard import cli
from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import COMMUNITY_PROJECTS, Config
from gerrit_dashboard.fetcher import GerritFetcher
from gerrit_dashboard.store import user_store


def community_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.projects = list(COMMUNITY_PROJECTS)
    cfg.data_dir = tmp_path
    return cfg


class TestFetchBundleRestriction:
    def _fetcher(self, tmp_path):
        cfg = community_cfg(tmp_path)
        store = user_store(tmp_path, "mvef")
        f = GerritFetcher(cfg, store, target="mvef")
        f.whoami = lambda: dict(ME)
        f._client = lambda: SimpleNamespace(username="mvef")
        f.fetch_next_queues = lambda: {}
        f.enrich_threads = lambda *a, **k: {}
        return f, store

    def test_all_queries_carry_project_clause_and_watchlist_evicted(self, tmp_path):
        f, store = self._fetcher(tmp_path)
        fs_change = mk_change(number=101, project="fs/lustre-release")
        other_change = mk_change(number=90001, project="private/example-release",
                          branch="release-1")
        queries = []

        def fake_query_all(q, options=None):
            queries.append(q)
            return [fs_change] if q.startswith("owner:") else []

        f.query_all = fake_query_all
        f.fetch_change_status = lambda n: (
            (dict(other_change), "ok") if n == 90001 else (None, "missing"))
        store.watchlist_add("90001")

        bundle = f.fetch_bundle()

        assert queries, "no queries captured"
        for q in queries:
            assert "project:fs/lustre-release" in q, f"unrestricted query: {q}"
        assert 101 in bundle["changes"]
        assert 90001 not in bundle["changes"]
        assert 90001 not in bundle["roles"]
        assert all(p == "fs/lustre-release"
                   for p in (c.get("project") for c in bundle["changes"].values()))
        # Evicted from the persisted watchlist too — not refetched forever.
        assert store.load_watchlist() == []
        assert all(e["number"] != 90001 for e in bundle["watchlist"])
        # The message must NOT reveal that the change exists elsewhere.
        assert bundle["errors"] == ["watchlist change 90001 is not available here — removed"]

    def test_restricted_and_absent_changes_are_indistinguishable(self, tmp_path):
        """The watchlist must not become an existence oracle.

        Probing a number that lives in a project this instance does not
        serve has to look exactly like probing one that does not exist —
        same message, same resulting store state — or anyone could map
        out the restricted projects through the error banner.
        """
        outcomes = []
        for kind in ("restricted", "absent"):
            f, store = self._fetcher(tmp_path / kind)
            f.query_all = lambda q, options=None: []
            other_change = mk_change(number=90001, project="private/example-release")
            f.fetch_change_status = lambda n, kind=kind, c=other_change: (
                (dict(c), "ok") if kind == "restricted" else (None, "missing"))
            store.watchlist_add("90001")
            bundle = f.fetch_bundle()
            outcomes.append((bundle["errors"], store.load_watchlist(),
                             90001 in bundle["changes"]))
        assert outcomes[0] == outcomes[1], "restricted vs absent are distinguishable"

    def test_transient_failure_keeps_the_watchlist_entry(self, tmp_path):
        # A flaky fetch must NOT silently unwatch a legitimate change.
        f, store = self._fetcher(tmp_path)
        f.query_all = lambda q, options=None: []
        f.fetch_change_status = lambda n: (None, "error")
        store.watchlist_add("12345")
        bundle = f.fetch_bundle()
        assert [e["number"] for e in store.load_watchlist()] == [12345]
        assert any("could not be fetched" in e for e in bundle["errors"])

    def test_next_queue_fetch_skips_restricted_projects(self, tmp_path):
        cfg = community_cfg(tmp_path)
        store = user_store(tmp_path, "mvef")
        f = GerritFetcher(cfg, store, target="mvef")
        fetched = []

        def fake_retry(fn, what, tries=3):
            fetched.append(what)
            return {"log": []}

        f._with_retry = fake_retry
        queues = f.fetch_next_queues()
        assert list(queues) == [("fs/lustre-release", "master")]
        assert fetched == ["gitiles master-next"]

    def test_unrestricted_keeps_everything(self, tmp_path):
        f, store = self._fetcher(tmp_path)
        f.config.projects = []
        other_change = mk_change(number=90001, project="private/example-release")
        f.query_all = lambda q, options=None: (
            [other_change] if q.startswith("owner:") and "wip" not in q else [])
        f.fetch_change_status = lambda n: (None, "missing")
        bundle = f.fetch_bundle()
        assert 90001 in bundle["changes"]


class TestSnapshotProjectStamp:
    def test_snapshot_carries_filter_and_loader_rejects_mismatch(self, tmp_path):
        cfg = community_cfg(tmp_path)
        b = mk_bundle([(mk_change(number=101), {"mine"})])
        snap = build_snapshot(b, cfg)
        assert snap["projects"] == ["fs/lustre-release"]

        store = user_store(tmp_path, "mvef")
        store.save_snapshot(snap)
        assert store.load_snapshot(projects=["fs/lustre-release"]) is not None
        # An unrestricted (or differently restricted) loader must not
        # serve it — and the restricted loader must never serve an
        # unrestricted snapshot.
        assert store.load_snapshot(projects=[]) is None
        assert store.load_snapshot() is None

        cfg.projects = []
        open_snap = build_snapshot(b, cfg)
        store.save_snapshot(open_snap)
        assert store.load_snapshot(projects=["fs/lustre-release"]) is None
        assert store.load_snapshot() is not None


class TestCliProjectArgs:
    def test_community_flag(self):
        cfg = Config()
        cli.apply_project_args(cfg, argparse.Namespace(community=True, projects=None))
        assert cfg.projects == COMMUNITY_PROJECTS

    def test_projects_flag_overrides(self):
        cfg = Config()
        cli.apply_project_args(
            cfg, argparse.Namespace(community=True, projects="fs/lustre-release, fs/lnet"))
        assert cfg.projects == ["fs/lustre-release", "fs/lnet"]

    def test_no_flags_keep_env_config(self):
        cfg = Config()
        cfg.projects = ["from-env"]
        cli.apply_project_args(cfg, argparse.Namespace(community=False, projects=None))
        assert cfg.projects == ["from-env"]
