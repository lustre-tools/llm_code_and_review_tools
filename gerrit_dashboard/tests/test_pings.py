"""@-ping detection: mentions of me in unresolved comment threads.

The whole point is not to miss a ping in the Gerrit email flood — so the
detector must be precise (quotes and pasted trailers are not pings) and
a ping must live until the THREAD IS RESOLVED: my own reply downgrades
it from waiting-on-me to open, it never makes it disappear.
"""

from conftest import ADILGER, JANITOR, ME, mk_bundle, mk_change

from gerrit_dashboard.classify import build_snapshot
from gerrit_dashboard.config import Config
from gerrit_dashboard.fetcher import build_thread_buckets, mention_patterns

ALIASES = (ME["username"], ME["email"], ME["name"])   # mvef, mvef@whamcloud.com, Marc Vef


def comment(cid, author, message, *, reply_to=None, unresolved=True,
            updated="2026-08-19 10:00:00.000000000", line=5, ps=2):
    c = {"id": cid, "author": author, "message": message, "updated": updated,
         "unresolved": unresolved, "line": line, "patch_set": ps}
    if reply_to:
        c["in_reply_to"] = reply_to
    return c


def buckets(*comments_in_file, aliases=ALIASES):
    return build_thread_buckets({"a/b.c": list(comments_in_file)}, ME["_account_id"], aliases)


class TestMentionPatterns:
    def test_at_username(self):
        pats = mention_patterns(("mvef",))
        assert any(p.search("could you look @mvef ?") for p in pats)
        assert any(p.search("@mvef: ping") for p in pats)

    def test_bare_username_is_not_a_ping(self):
        pats = mention_patterns(("mvef",))
        assert not any(p.search("mvef uploaded a new patchset") for p in pats)

    def test_email_with_and_without_at(self):
        pats = mention_patterns(("mvef@whamcloud.com",))
        assert any(p.search("adding @mvef@whamcloud.com for the DNE part") for p in pats)
        assert any(p.search("mvef@whamcloud.com should confirm") for p in pats)

    def test_other_users_email_does_not_match(self):
        pats = mention_patterns(("mvef",))
        # '@mvef' inside another address is a domain, not a mention
        assert not any(p.search("send it to build@mvef.example.org") for p in pats)

    def test_full_name_case_insensitive(self):
        pats = mention_patterns(("Marc Vef",))
        assert any(p.search("as discussed with marc vef earlier") for p in pats)

    def test_single_word_name_only_matches_at_prefixed(self):
        # A one-word name is treated like a username: "@Marc" pings,
        # bare "Marc" in prose would be far too noisy.
        pats = mention_patterns(("Marc",))
        assert any(p.search("@Marc please look") for p in pats)
        assert not any(p.search("Marc uploaded a new patchset") for p in pats)

    def test_blank_aliases_skipped(self):
        assert mention_patterns(("", None, "  ")) == []


class TestPingDetection:
    def test_unanswered_at_mention_is_a_ping(self):
        b = buckets(comment("c1", ADILGER, "@mvef could you rebase this?"))
        assert b["pings"] == 1
        assert b["pings_open"] == 1
        assert b["ping_items"][0]["answered"] is False
        item = b["ping_items"][0]
        assert item["kind"] == "ping"
        assert item["author"] == ADILGER["name"]
        assert "@mvef" in item["snippet"]
        assert item["conv"]

    def test_answered_mention_downgrades_but_stays_listed(self):
        """The 68073 regression: "Apologies, I missed that first ping...
        I'll look tomorrow" must NOT make the ping disappear — only the
        thread being resolved does.  A reply downgrades it from
        waiting-on-you to open."""
        b = buckets(
            comment("c1", ADILGER, "@mvef could you rebase this?",
                    updated="2026-08-19 10:00:00.000000000"),
            comment("c2", ME, "will look tomorrow", reply_to="c1",
                    updated="2026-08-19 11:00:00.000000000"))
        assert b["pings"] == 0            # not waiting on me
        assert b["pings_open"] == 1       # but still on the list
        assert b["ping_items"][0]["answered"] is True

    def test_mention_after_my_reply_pings_again(self):
        b = buckets(
            comment("c1", ADILGER, "@mvef could you rebase this?",
                    updated="2026-08-19 10:00:00.000000000"),
            comment("c2", ME, "done", reply_to="c1",
                    updated="2026-08-19 11:00:00.000000000"),
            comment("c3", ADILGER, "@mvef sorry, one more thing", reply_to="c2",
                    updated="2026-08-19 12:00:00.000000000"))
        assert b["pings"] == 1
        assert "one more thing" in b["ping_items"][0]["snippet"]

    def test_resolved_thread_clears_the_ping_entirely(self):
        b = buckets(comment("c1", ADILGER, "@mvef ping", unresolved=False))
        assert b["pings"] == 0
        assert b["pings_open"] == 0
        assert b["ping_items"] == []

    def test_quoted_mention_is_not_a_ping(self):
        b = buckets(comment("c1", ADILGER, "> @mvef could you rebase\nI already did that"))
        assert b["pings"] == 0

    def test_pasted_trailer_is_not_a_ping(self):
        b = buckets(comment(
            "c1", ADILGER, "picking this up.\nReviewed-by: Marc Vef <mvef@whamcloud.com>"))
        assert b["pings"] == 0

    def test_full_name_in_prose_is_a_ping(self):
        b = buckets(comment("c1", ADILGER, "Marc Vef should double-check the quota part"))
        assert b["pings"] == 1

    def test_self_mention_is_not_a_ping(self):
        b = buckets(comment("c1", ME, "note to self: @mvef fix this tomorrow"))
        assert b["pings"] == 0

    def test_bot_text_is_not_a_ping(self):
        b = buckets(comment("c1", JANITOR, "retest requested by @mvef"))
        assert b["pings"] == 0

    def test_one_ping_per_thread_newest_wins(self):
        b = buckets(
            comment("c1", ADILGER, "@mvef first ask",
                    updated="2026-08-19 10:00:00.000000000"),
            comment("c2", ADILGER, "@mvef again, please", reply_to="c1",
                    updated="2026-08-19 12:00:00.000000000"))
        assert b["pings"] == 1
        assert "again" in b["ping_items"][0]["snippet"]

    def test_snippet_is_the_mentioning_line(self):
        b = buckets(comment(
            "c1", ADILGER,
            "Some context first.\nMore context.\n@mvef what do you think about the grant math?"))
        assert b["ping_items"][0]["snippet"].startswith("@mvef what do you think")

    def test_no_aliases_no_scan(self):
        b = buckets(comment("c1", ADILGER, "@mvef ping"), aliases=())
        assert b["pings"] == 0


class TestPingsInSnapshot:
    def _threads(self, number, author=ADILGER):
        return {number: {
            "my_turn": 1, "their_turn": 0, "sticky": 0, "bot": 0,
            "pings": 1, "pings_open": 1,
            "items": [], "ping_items": [{
                "kind": "ping", "file": "a/b.c", "line": 5, "ps": 2, "answered": False,
                "author": author["name"], "updated": "2026-08-19 10:00:00.000000000",
                "snippet": "@mvef could you check?", "conv": []}],
        }}

    def test_ping_is_p1_action_and_pinged_section(self):
        c = mk_change(number=101, unresolved=1)
        s = build_snapshot(mk_bundle([(c, {"cc"})], threads=self._threads(101)), Config())
        assert s["kpis"]["pings"] == 1
        assert [r["number"] for r in s["pinged"]] == [101]
        r = s["pinged"][0]
        assert any(i["rule"] == "mentioned" and i["prio"] == 1 for i in r["items"])
        assert "pinged you" in r["top_reason"] or any(
            "pinged you" in i["reason"] for i in r["items"])
        # P1 → also lands in the action list
        assert 101 in [x["number"] for x in s["action"] + s["action_old"]]

    def test_hidden_patch_ping_not_counted_in_kpi(self):
        c = mk_change(number=101, unresolved=1)
        b = mk_bundle([(c, {"cc"})], threads=self._threads(101))
        b["hidden"] = {101}
        s = build_snapshot(b, Config())
        assert s["kpis"]["pings"] == 0
        assert [r["number"] for r in s["pinged"]] == [101]  # still listed, marked hidden

    def test_answered_open_ping_stays_listed_as_informational(self):
        # 68073: replied "will look tomorrow" — still in the Mentions tab,
        # no longer a P1 act-now item.
        c = mk_change(number=103, unresolved=1)
        threads = self._threads(103)
        threads[103]["pings"] = 0
        threads[103]["ping_items"][0]["answered"] = True
        s = build_snapshot(mk_bundle([(c, {"review"})], threads=threads), Config())
        assert s["kpis"]["pings"] == 0
        assert s["kpis"]["pings_open"] == 1
        assert [r["number"] for r in s["pinged"]] == [103]
        r = s["pinged"][0]
        item = next(i for i in r["items"] if i["rule"] == "mentioned-open")
        assert item["prio"] == 3
        assert "not resolved" in item["reason"]

    def test_ping_on_wip_patch_still_surfaces(self):
        c = mk_change(number=102, wip=True, unresolved=1)
        s = build_snapshot(mk_bundle([(c, {"mine"})], threads=self._threads(102)), Config())
        assert s["kpis"]["pings"] == 1
        assert 102 in [x["number"] for x in s["action"] + s["action_old"]]
