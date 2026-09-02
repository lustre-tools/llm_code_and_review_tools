"""Deployment knobs: everything site-specific must come from config.

The defaults describe a generic Lustre-CI Gerrit; a site adapts it with
GD_* variables instead of editing the source.
"""

import pytest

from conftest import ME, mk_bundle, mk_change, vote

from gerrit_dashboard import ci_parse
from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import Config


class TestDefaults:
    def test_nothing_site_specific_is_baked_in(self, monkeypatch):
        for var in ("GD_NEXT_QUEUES", "GD_VERIFIED_OVERRIDE", "GD_BRANCH_COLORS",
                    "GD_PROJECTS", "GD_COMMUNITY", "GD_BOARDS",
                    "GD_DEFAULT_USER", "GERRIT_USER"):
            monkeypatch.delenv(var, raising=False)
        cfg = Config.from_env()
        assert cfg.default_user == ""          # falls back to GERRIT_USER
        assert cfg.verified_override_emails == []
        assert cfg.branch_colors == {}
        assert cfg.projects == [] and cfg.boards == []
        assert cfg.next_queues == [("fs/lustre-release", "master")]

    def test_default_user_follows_the_credential(self, monkeypatch):
        monkeypatch.delenv("GD_DEFAULT_USER", raising=False)
        monkeypatch.setenv("GERRIT_USER", "someone")
        assert Config.from_env().default_user == "someone"


class TestWebChrome:
    def test_theme_defaults_to_dark(self, monkeypatch):
        monkeypatch.delenv("GD_DEFAULT_THEME", raising=False)
        assert Config.from_env().default_theme == "dark"

    @pytest.mark.parametrize("value", ["light", "gruvbox-light", "dark"])
    def test_theme_from_env(self, monkeypatch, value):
        monkeypatch.setenv("GD_DEFAULT_THEME", value.upper())  # case-insensitive
        assert Config.from_env().default_theme == value

    def test_bad_theme_falls_back_to_dark(self, monkeypatch):
        monkeypatch.setenv("GD_DEFAULT_THEME", "solarized")
        assert Config.from_env().default_theme == "dark"

    def test_site_link_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("GD_SITE_NAME", raising=False)
        monkeypatch.delenv("GD_SITE_HOME", raising=False)
        cfg = Config.from_env()
        assert cfg.site_name == "" and cfg.site_home == ""

    def test_site_link_from_env(self, monkeypatch):
        monkeypatch.setenv("GD_SITE_NAME", "Example Lustre Tools")
        monkeypatch.setenv("GD_SITE_HOME", "/")
        cfg = Config.from_env()
        assert cfg.site_name == "Example Lustre Tools" and cfg.site_home == "/"


class TestNextQueues:
    def test_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("GD_NEXT_QUEUES", "a/b:master, c/d:release-1")
        assert Config.from_env().next_queues == [("a/b", "master"), ("c/d", "release-1")]

    def test_malformed_entries_ignored(self, monkeypatch):
        monkeypatch.setenv("GD_NEXT_QUEUES", "a/b:master,nonsense,:x,y:")
        assert Config.from_env().next_queues == [("a/b", "master")]

    def test_project_with_colon_free_branch(self, monkeypatch):
        # rpartition keeps a project name containing a colon intact.
        monkeypatch.setenv("GD_NEXT_QUEUES", "host:port/proj:master")
        assert Config.from_env().next_queues == [("host:port/proj", "master")]


class TestVerifiedOverride:
    def test_env_parsing_is_lowercased(self, monkeypatch):
        monkeypatch.setenv("GD_VERIFIED_OVERRIDE", "A@x.com, B@y.com")
        assert Config.from_env().verified_override_emails == ["a@x.com", "b@y.com"]

    def test_gate_uses_configured_emails_only(self):
        from conftest import JENKINS
        c = mk_change(verified=[vote(JENKINS, 1), vote(ME, 1)])
        assert ci_parse.verified_gate(c) == "PENDING"
        assert ci_parse.verified_gate(c, [ME["email"].upper()]) == "OK"

    def test_override_never_beats_a_negative(self):
        from conftest import JENKINS, MALOO
        c = mk_change(verified=[vote(JENKINS, 1), vote(ME, 1), vote(MALOO, -1)])
        assert ci_parse.verified_gate(c, [ME["email"]]) == "FAIL"


class TestBranchColors:
    def test_only_known_tokens_survive(self, monkeypatch):
        monkeypatch.setenv("GD_BRANCH_COLORS", "a:tag,b:run,c:notacolor,d:")
        assert Config.from_env().branch_colors == {"a": "tag", "b": "run"}

    def test_map_reaches_the_snapshot(self):
        cfg = Config()
        cfg.branch_colors = {"release-1": "tag"}
        snap = build_snapshot(mk_bundle([(mk_change(), {"mine"})]), cfg)
        assert snap["branch_colors"] == {"release-1": "tag"}


class TestReviewThresholds:
    @pytest.mark.parametrize("threshold,voters,expected", [(2, 1, False), (2, 2, True), (1, 1, True)])
    def test_native_threshold_is_configurable(self, threshold, voters, expected):
        from gerrit_dashboard.review_rules import review_state
        reviewers = [{"_account_id": 500 + i, "value": 1} for i in range(voters)]
        c = mk_change(code_review=reviewers)
        assert review_state(c, threshold, 1)["pass"] is expected

    def test_backport_threshold_applies_to_backports(self):
        from gerrit_dashboard.review_rules import is_backport, review_state
        msg = "subject\n\nLustre-change: https://review.example.com/c/x/+/1234\nChange-Id: I1\n"
        c = mk_change(commit_message=msg, code_review=[{"_account_id": 501, "value": 1}])
        assert is_backport(c)
        assert review_state(c, 2, 1)["pass"] is True     # 1 is enough
        assert review_state(c, 2, 2)["pass"] is False    # unless site wants 2
