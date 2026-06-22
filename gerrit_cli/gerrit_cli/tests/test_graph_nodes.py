"""Tests for the graph/nodes.py construction + enrichment helpers.

`_make_node` builds the initial node dict used by the HTML template;
`_update_node_meta` enriches it once the bulk revision fetch returns
DETAILED_ACCOUNTS. The contract with the JS panel depends on a small
set of keys (id, status, current_patchset, author, owner, ...) being
present and correct, and on the owner backfill happening so the
review-health logic can distinguish change owner from git author.
"""

from gerrit_cli.graph.nodes import _make_node, _update_node_meta


class TestMakeNodeShape:
    def test_returns_dict_with_all_expected_keys(self):
        node = _make_node(
            cn=123, subject="LU-19921 lmv: x", status="NEW",
            latest=5, author="Di Wang",
            base_url="https://review.whamcloud.com",
        )
        for key in [
            "id", "subject", "status", "current_patchset", "author",
            "owner", "current_commit", "is_backport", "url", "ticket",
            "topic", "hashtags", "checkout_cmd", "cherrypick_cmd",
            "updated", "is_wip", "project", "branch",
        ]:
            assert key in node, f"missing key {key!r}"

    def test_owner_defaults_to_empty_string(self):
        """The constructor cannot know the change owner — only
        DETAILED_ACCOUNTS gives us that. The default must be ""
        so _update_node_meta can backfill, and so the JS health
        check can fall back to author when owner is missing."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        assert node["owner"] == ""

    def test_current_commit_defaults_to_empty_string(self):
        """The latest patchset's SHA is only known after the bulk
        revision fetch returns it. Default must be "" so the JS
        "Commit hash" button hides itself until the data lands."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        assert node["current_commit"] == ""


class TestMakeNodeTicketExtraction:
    def test_extracts_lu_ticket_from_subject(self):
        node = _make_node(
            cn=1, subject="LU-12345 osd: do stuff",
            status="NEW", latest=1, author="A", base_url="https://x",
        )
        assert node["ticket"] == "LU-12345"

    def test_explicit_ticket_argument_wins(self):
        """Caller-supplied ticket overrides regex extraction —
        used when the subject doesn't start with a ticket prefix."""
        node = _make_node(
            cn=1, subject="osd: tweak", ticket="LU-99999",
            status="NEW", latest=1, author="A", base_url="https://x",
        )
        assert node["ticket"] == "LU-99999"

    def test_empty_ticket_when_subject_has_no_match(self):
        node = _make_node(
            cn=1, subject="just a subject", status="NEW",
            latest=1, author="A", base_url="https://x",
        )
        assert node["ticket"] == ""


class TestMakeNodeUrls:
    def test_url_uses_project_path(self):
        node = _make_node(
            cn=42, subject="S", status="NEW", latest=1, author="A",
            base_url="https://review.whamcloud.com",
            project="ex/lustre-release",
        )
        assert node["url"] == (
            "https://review.whamcloud.com/c/ex/lustre-release/+/42"
        )

    def test_checkout_cmd_includes_refs_path(self):
        """Gerrit's refs path format is refs/changes/NN/<cn>/<ps>
        where NN is the last two digits of cn. The checkout/cherry-
        pick commands depend on that being right."""
        node = _make_node(
            cn=12345, subject="S", status="NEW", latest=7, author="A",
            base_url="https://review.whamcloud.com",
        )
        assert "refs/changes/45/12345/7" in node["checkout_cmd"]
        assert "refs/changes/45/12345/7" in node["cherrypick_cmd"]


class TestUpdateNodeMetaOwnerBackfill:
    """The owner backfill is what makes the review-health "exclude
    only the OWNER's self-vote" rule work. Until DETAILED_ACCOUNTS
    arrives, owner is "" — and the JS falls back to author. After,
    owner is set and overrides the fallback."""

    def test_backfills_owner_from_change_payload(self):
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1,
            author="Sebastien Buisson", base_url="https://x",
        )
        _update_node_meta(node, {
            "owner": {"name": "Marc Vef"},
            "topic": "", "hashtags": [], "updated": "",
        })
        assert node["owner"] == "Marc Vef"
        # author untouched — owner is a separate concept.
        assert node["author"] == "Sebastien Buisson"

    def test_missing_owner_leaves_prior_value_intact(self):
        """A change payload missing the owner.name field must not
        wipe out a previously-set owner — otherwise a partial
        re-enrichment could clear good data."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        node["owner"] = "Marc Vef"
        _update_node_meta(node, {
            "owner": {},  # no "name" key
            "topic": "", "hashtags": [], "updated": "",
        })
        assert node["owner"] == "Marc Vef"

    def test_backfills_current_commit_from_change_payload(self):
        """The bulk revision fetch's response includes
        `current_revision` (the latest patchset's SHA) — copy it
        onto the node so the panel's "Commit hash" button has
        something to put on the clipboard."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        _update_node_meta(node, {
            "current_revision": "abc123def456",
            "topic": "", "hashtags": [], "updated": "",
        })
        assert node["current_commit"] == "abc123def456"

    def test_missing_current_revision_leaves_prior_value_intact(self):
        """Partial payload (no current_revision) must not wipe out
        a previously-set commit hash."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        node["current_commit"] = "abc123"
        _update_node_meta(node, {
            "topic": "", "hashtags": [], "updated": "",
        })
        assert node["current_commit"] == "abc123"


def _payload_with_message(rev_hash, message):
    """Build a change payload shaped like the ALL_REVISIONS +
    ALL_COMMITS response — the only field that matters for these
    tests is the current revision's commit message."""
    return {
        "current_revision": rev_hash,
        "revisions": {
            rev_hash: {
                "_number": 1,
                "commit": {"message": message},
            },
        },
    }


class TestUpdateNodeMetaBackportDetection:
    """The `is_backport` flag drives a different ready-to-land
    rule on the JS side (1 reviewer +1 instead of 2). Detection
    mirrors patch_status: any commit-message trailer of the form
    `Lustre-change: <url>` means the patch is a backport."""

    def test_lustre_change_trailer_marks_node_as_backport(self):
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        message = (
            "LU-19921 osd: foo bar\n\n"
            "Backport of master change.\n\n"
            "Lustre-change: https://review.whamcloud.com/12345\n"
            "Lustre-commit: abc123def456\n"
            "Change-Id: I12345\n"
            "Signed-off-by: Marc Vef <mvef@ddn.com>\n"
        )
        _update_node_meta(node, _payload_with_message("rev1", message))
        assert node["is_backport"] is True

    def test_message_without_trailer_is_native_patch(self):
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        message = (
            "LU-19921 osd: foo bar\n\n"
            "Some change description.\n\n"
            "Change-Id: I12345\n"
            "Signed-off-by: Marc Vef <mvef@ddn.com>\n"
        )
        _update_node_meta(node, _payload_with_message("rev1", message))
        assert node["is_backport"] is False

    def test_lustre_commit_tbd_still_counts_as_backport(self):
        """When the master change hasn't merged yet, the trailer
        carries `Lustre-commit: TBD`. The Lustre-change trailer
        alone is the marker — matches patch_status's rule."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        message = (
            "LU-19921 osd: foo bar\n\n"
            "Lustre-change: https://review.whamcloud.com/12345\n"
            "Lustre-commit: TBD\n"
            "Change-Id: I12345\n"
        )
        _update_node_meta(node, _payload_with_message("rev1", message))
        assert node["is_backport"] is True

    def test_inline_mention_of_lustre_change_does_not_count(self):
        """The trailer must be its own line — a casual reference
        to "Lustre-change:" inside prose shouldn't trip the
        detector."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        message = (
            "Note: the Lustre-change: prefix is also used by "
            "backports.\n\n"
            "Change-Id: I12345\n"
        )
        _update_node_meta(node, _payload_with_message("rev1", message))
        assert node["is_backport"] is False

    def test_no_message_leaves_default(self):
        """A payload without revisions/commit info (e.g. the
        slim discovered-nodes fetch) leaves is_backport at its
        default False — we don't crash, we just don't know."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        _update_node_meta(node, {})
        assert node["is_backport"] is False


class TestUpdateNodeMetaCopiesFields:
    def test_copies_topic_hashtags_updated_wip(self):
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
        )
        _update_node_meta(node, {
            "topic": "feature-x",
            "hashtags": ["master-next", "blocked"],
            "updated": "2026-06-01 10:00:00.000000000",
            "work_in_progress": True,
        })
        assert node["topic"] == "feature-x"
        assert node["hashtags"] == ["master-next", "blocked"]
        assert node["updated"] == "2026-06-01 10:00:00.000000000"
        assert node["is_wip"] is True

    def test_project_and_branch_overrideable(self):
        """/related entries don't include the branch — the bulk
        revision fetch is where it lands. Confirm the override
        happens when the change payload supplies them."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x", project="fs/lustre-release", branch="",
        )
        _update_node_meta(node, {
            "project": "ex/lustre-release", "branch": "b_es",
        })
        assert node["project"] == "ex/lustre-release"
        assert node["branch"] == "b_es"

    def test_blank_project_or_branch_preserved(self):
        """If the change payload doesn't carry project/branch,
        don't blank out values set at construction time."""
        node = _make_node(
            cn=1, subject="S", status="NEW", latest=1, author="A",
            base_url="https://x",
            project="fs/lustre-release", branch="master",
        )
        _update_node_meta(node, {})
        assert node["project"] == "fs/lustre-release"
        assert node["branch"] == "master"
