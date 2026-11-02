"""Meta/documentation commands: search, explain, examples, describe."""

import sys

from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, error_code_for, output_error, output_success


# Command explanations with detailed usage and examples
COMMAND_EXPLANATIONS = {
    "check": {
        "summary": "Check Gerrit configuration and authentication",
        "description": """
Check authentication with a read-only GET /accounts/self request and a
10-second network timeout. The result reports the active configuration file
and a sanitized status; it never returns credentials or publishes comments.
Successful authentication does not establish project publishing permissions.

Use --offline to locate the configuration file without contacting Gerrit.
Exit codes: 0 success, 2 missing or rejected authentication, 4 invalid server
URL, 5 connection failure, and 1 other failures.
""",
        "examples": [
            {"command": "gc check", "description": "Verify Gerrit authentication"},
            {"command": "gc check --offline", "description": "Locate the CLI configuration without network access"},
        ],
        "related": ["info", "review"],
    },
    "comments": {
        "summary": "Get unresolved comments from a Gerrit change",
        "description": """
The 'comments' command extracts comment threads from a Gerrit change URL.
By default, it only shows unresolved comments. Use --all to include resolved ones.

Each comment thread carries its index (0, 1, 2, ...) as "index", which you
can use with other commands like 'reply' or 'stage'. Those commands index the
default listing. With --all, the threads the default listing shows come first,
with the same indices, and the resolved ones follow; 'reply', 'done', 'ack'
and 'stage' refuse an index of one of those. Answer a resolved thread with
'gc reply' or 'gc batch' and its root_comment.id.

Indices shift: once a reply resolves thread 0, the next listing's thread 0
is what was thread 1. For more than one reply, address threads by
root_comment.id ('gc reply <id> ...'), which does not shift.

review_messages holds the change-level messages. A review message that
says nothing beyond its vote and "(N comments)" is left out by default;
--include-system keeps every message, so each vote shows with its
reviewer and patchset, along with uploads, rebases and CI results.
--messages lists only the change messages, oldest first, with date,
patchset, author and text: has anyone asked for a BUILD yet?
""",
        "examples": [
            {
                "command": "gc comments https://review.example.com/c/project/+/12345",
                "description": "Get all unresolved comments (JSON output)",
            },
            {
                "command": "gc comments --pretty https://review.example.com/c/project/+/12345",
                "description": "Get comments with human-readable JSON",
            },
            {
                "command": "gc comments --all https://review.example.com/c/project/+/12345",
                "description": "Include resolved comments",
            },
            {
                "command": "gc comments --no-context https://review.example.com/c/project/+/12345",
                "description": "Skip code context around comments",
            },
            {
                "command": "gc comments --messages 12345",
                "description": "The change messages, oldest first (CI too with --include-ci)",
            },
            {
                "command": "gc comments --all --include-system 12345",
                "description": "Every review message, votes and CI included",
            },
        ],
        "related": ["reply", "stage", "series-comments"],
    },
    "reply": {
        "summary": "Reply to a comment thread",
        "description": """
The 'reply' command posts a reply to a specific comment thread. You identify
the thread by its index from the 'comments' output, or by the id of a comment
in it (root_comment.id in 'comments'). An index of a resolved thread, which
only 'comments --all' lists, is refused; a comment id reaches any thread.

An index is looked up afresh in the unresolved listing each time, so after a
reply that resolves a thread, the indices after it shift down by one: a second
'gc reply 1' then answers what 'gc comments' had shown as 2. When you reply
to more than one thread, use the comment ids, or 'gc batch'.

URL is optional - if you recently ran 'gc comments URL', the URL is remembered
and reused automatically. Use --url to override.

Common patterns:
- Use --done to mark a comment as addressed (adds "Done" and resolves)
- Use --ack to acknowledge without action (adds "Acknowledged" and resolves)
- Use --resolve with a custom message to resolve the thread
""",
        "examples": [
            {
                "command": "gc reply 0 \"Fixed in the latest patchset\"",
                "description": "Reply to thread 0 (uses last URL from 'gc comments')",
            },
            {
                "command": "gc reply 0 --done",
                "description": "Mark thread 0 as done (resolved)",
            },
            {
                "command": "gc reply 2 --ack",
                "description": "Acknowledge thread 2",
            },
            {
                "command": "gc reply 1 \"Will fix\" --resolve",
                "description": "Reply and resolve with custom message",
            },
            {
                "command": "gc reply 0 --done --url URL",
                "description": "Explicit URL (overrides remembered URL)",
            },
            {
                "command": "gc reply fe4b1c39_3f8c9a2e \"Fixed\" --resolve",
                "description": "Reply by the thread's root_comment.id; stable across replies",
            },
        ],
        "related": ["comments", "stage", "batch"],
    },
    "stage": {
        "summary": "Stage a comment reply for later posting",
        "description": """
The 'stage' command queues a reply without immediately posting it. This is
useful when addressing multiple comments - you can stage all replies and
then post them together with 'push'.

If you're in an active session (from review-series), the URL is optional.
""",
        "examples": [
            {
                "command": "gc stage 0 \"Fixed\"",
                "description": "Stage a reply to thread 0 (uses session URL)",
            },
            {
                "command": "gc stage --done 1",
                "description": "Stage thread 1 as done",
            },
            {
                "command": "gc stage --url URL 2 \"Will address later\"",
                "description": "Stage with explicit URL",
            },
        ],
        "related": ["push", "staged", "reply"],
    },
    "push": {
        "summary": "Post all staged comment replies",
        "description": """
The 'push' command posts all staged replies to Gerrit. Use --dry-run to
preview what would be posted without actually sending.

Each reply goes in its thread, on the patchset its parent comment was
made on, and all of them are posted together as one message. Replies
on several patchsets go the way the web UI does it: drafts first, then
one publish. Publishing takes every draft of yours on the change, so
push then refuses while you have other drafts there; publish or delete
them first. If anything fails, nothing is posted and the replies stay
staged. With GERRIT_CLI_MODE=drafts the replies are left as drafts.

It does not push commits. To upload a patchset, use 'upload'.
""",
        "examples": [
            {
                "command": "gc push 12345",
                "description": "Push staged replies for change 12345",
            },
            {
                "command": "gc push --dry-run 12345",
                "description": "Preview what would be pushed",
            },
            {
                "command": "gc push",
                "description": "Push all staged replies for all changes",
            },
        ],
        "related": ["stage", "staged"],
    },
    "upload": {
        "summary": "Upload HEAD (or a series ending at HEAD) to Gerrit",
        "description": """
The 'upload' command pushes HEAD to refs/for/<branch> over HTTPS, as the
account of the credential set --user selects. The password reaches git
through a temporary GIT_ASKPASS script reading it from the environment:
never in the URL, never on a command line. Configured credential helpers
are switched off for the push.

By default it uploads exactly one commit. With CHANGE (number, URL or
Change-Id), it refuses unless HEAD's Change-Id trailer is that change's
-- the guard against uploading a diff as a patchset of some other
change. Without CHANGE, HEAD's Change-Id names the change. A Change-Id
Gerrit does not know is uploaded as a new change, only when --project
and --branch are both given. If more than one commit between the branch
and HEAD is new to Gerrit, it refuses and lists them.

--series uploads all of those commits, each to the change its own
Change-Id names (updating it, or creating it). Every commit needs a
Change-Id, and CHANGE, if given, must be one of them rather than HEAD.

Changes go up public. --private uploads them private, naming Jenkins
(devops@whamcloud.com) and Maloo (maloo@whamcloud.com) as reviewers in
the push so they can build and test the first patchset;
--no-ci-reviewers leaves them out, and CI then neither builds nor tests
the change. Updating a
change that is already private is refused (PRIVATE_CHANGE) unless
--private keeps it private or --public makes it public.

Two commits between the branch and HEAD with one Change-Id are refused
(DUPLICATE_CHANGE_ID): Gerrit would reject the push as "same Change-Id
in multiple changes". It usually means a commit was made on top of an
earlier patchset instead of amending it; the message gives the command
that squashes them.

A commit whose committer email is not registered to the account is
rewritten so the committer is the account's name and preferred email --
and so is every commit above it, since its parent changes. Author, tree
and message are untouched, and the result lists every rewrite.
--no-amend refuses instead.

--dry-run does every check and lookup, and prints the exact git push,
password redacted, plus what each commit would update or create, without
pushing or rewriting anything.
""",
        "examples": [
            {
                "command": "gc --user patrickbot upload 12345 --dry-run",
                "description": "Check everything and show the push, as the bot",
            },
            {
                "command": "gc --user patrickbot upload 12345",
                "description": "Upload HEAD as the next patchset of 12345",
            },
            {
                "command": "gc --user patrickbot upload 12345 --series --dry-run",
                "description": "Show what each commit of the series would update or create",
            },
            {
                "command": "gc upload --project fs/lustre-release --branch b2_15",
                "description": "Upload HEAD as a new change on b2_15",
            },
        ],
        "related": ["info", "set-topic", "push"],
    },
    "staged": {
        "summary": "Manage staged comment replies",
        "description": """
The 'staged' command group helps you view and manage queued replies.

Subcommands:
- list: Show all staged operations
- show <change>: Show staged ops for a specific change
- remove <change> <index>: Remove a specific staged reply
- clear [change]: Clear staged replies (all or for one change)
- refresh <change>: Update patchset number after amending
""",
        "examples": [
            {
                "command": "gc staged list",
                "description": "List all staged operations",
            },
            {
                "command": "gc staged show 12345",
                "description": "Show staged ops for change 12345",
            },
            {
                "command": "gc staged remove 12345 0",
                "description": "Remove first staged op for change 12345",
            },
            {
                "command": "gc staged clear",
                "description": "Clear all staged operations",
            },
            {
                "command": "gc staged refresh 12345",
                "description": "Update patchset after amending",
            },
        ],
        "related": ["stage", "push"],
    },
    "review-series": {
        "summary": "Start reviewing a patch series",
        "description": """
The 'review-series' command is the main entry point for AI-assisted patch
series review. It finds all related patches, shows comment counts, and
optionally checks out the first patch with comments.

This command:
1. Finds all patches in the series (following relations)
2. Counts unresolved comments on each patch
3. Checks out the first patch with comments (unless --no-checkout)
4. Starts a session for tracking your progress
5. Returns the workflow as review_prompt (unless --no-prompt)
""",
        "examples": [
            {
                "command": "gc review-series https://review.example.com/c/project/+/12345",
                "description": "Start reviewing - checkout first patch with comments",
            },
            {
                "command": "gc review-series --no-checkout URL",
                "description": "Just show series info without checkout",
            },
            {
                "command": "gc review-series --urls-only URL",
                "description": "List patch URLs only (plain text)",
            },
            {
                "command": "gc review-series --numbers-only URL",
                "description": "List change numbers only",
            },
        ],
        "related": ["work-on-patch", "finish-patch", "status"],
    },
    "work-on-patch": {
        "summary": "Start working on a specific patch",
        "description": """
Checkout a specific patch and show its comments. Accepts a change number
or a full Gerrit URL. When given a change number, the URL is derived from
the GERRIT_URL environment variable. Fetches from Gerrit automatically if
the patch is not already in the local git history.
""",
        "examples": [
            {
                "command": "gc work-on-patch 12345",
                "description": "Checkout change 12345 (derives URL from GERRIT_URL)",
            },
            {
                "command": "gc work-on-patch https://review.whamcloud.com/12345",
                "description": "Checkout change 12345 using full URL",
            },
        ],
        "related": ["review-series", "next-patch", "finish-patch"],
    },
    "finish-patch": {
        "summary": "Finish current patch and rebase the series",
        "description": """
After making changes and staging replies, use 'finish-patch' to:
1. Rebase all dependent patches on your changes
2. Push the series to Gerrit if the commit changed: under --user through
   'upload', as that account; otherwise with git push to the Gerrit remote,
   as whoever its URL logs in as
3. Auto-advance to the next patch with comments (unless --stay)

Always commit your changes before running this command.
""",
        "examples": [
            {
                "command": "gc finish-patch",
                "description": "Finish patch, rebase series, advance to next",
            },
            {
                "command": "gc finish-patch --stay",
                "description": "Finish and rebase, but stay on current patch",
            },
        ],
        "related": ["work-on-patch", "next-patch", "abort"],
    },
    "next-patch": {
        "summary": "Move to the next patch in the series",
        "description": """
Skip to the next patch without rebasing. Use this when you don't have
changes on the current patch or want to review without modifying.
""",
        "examples": [
            {
                "command": "gc next-patch",
                "description": "Move to the next patch",
            },
            {
                "command": "gc next-patch --with-comments",
                "description": "Skip to next patch that has comments",
            },
        ],
        "related": ["work-on-patch", "finish-patch", "status"],
    },
    "status": {
        "summary": "Show current session status",
        "description": """
Display information about the active session: which patch you're on,
remaining patches, staged replies, etc.
""",
        "examples": [
            {
                "command": "gc status",
                "description": "Show current session status",
            },
        ],
        "related": ["review-series", "work-on-patch"],
    },
    "abort": {
        "summary": "End the current session",
        "description": """
End the session and optionally discard changes. By default, this restores
the original git state. Use --keep-changes to preserve your work.
""",
        "examples": [
            {
                "command": "gc abort",
                "description": "End session and discard changes",
            },
            {
                "command": "gc abort --keep-changes",
                "description": "End session but keep git state",
            },
        ],
        "related": ["status", "review-series"],
    },
    "review": {
        "summary": "Get code changes for review",
        "description": """
Get the diff and file changes from a Gerrit change for code review.
Can also post review comments from a JSON file with --post-comments.

The JSON file may contain "message", "vote", "tag", and "comments".
Comments are either a flat list ([{path, line, message, unresolved}])
or the Gerrit REST format ({path: [{line, message, unresolved}]}).
Explicit CLI --message/--vote/--tag flags override the file values.
The message is posted as a resolved patchset-level comment, as the web
UI's reply box does, so it can be replied to; --no-thread posts it as a
bare change message instead.
Use --prefix to prepend text (e.g. a bot name) to the message and
every comment, and --dry-run to preview the payload without posting.
""",
        "examples": [
            {
                "command": "gc review URL",
                "description": "Get changes for review (JSON)",
            },
            {
                "command": "gc review --pretty URL",
                "description": "Get changes with readable JSON",
            },
            {
                "command": "gc review --full-content URL",
                "description": "Include full file contents",
            },
            {
                "command": "gc review --post-comments review.json URL",
                "description": "Post review comments from file",
            },
            {
                "command": "gc review --post-comments review.json --prefix '[Marc Bot]' --dry-run URL",
                "description": "Preview prefixed comments without posting",
            },
        ],
        "related": ["comments", "review-series", "diff"],
    },
    "diff": {
        "summary": "Show what changed between two patchsets",
        "description": """
Compare two patchsets of one change, for re-review after an update.
With no patchsets it compares the newest patchset with the one before
it; with one, that patchset with the newest. A change with only one
patchset is shown against its parent commit, the same diff 'gc review'
gives. Diffs carry 3 lines of context around each change (--unified N).

To read a change itself, use 'gc review'.
""",
        "examples": [
            {
                "command": "gc diff URL",
                "description": "Newest patchset against the one before it",
            },
            {
                "command": "gc diff URL 3",
                "description": "Patchset 3 against the newest",
            },
            {
                "command": "gc diff URL 3 5",
                "description": "Patchset 3 against patchset 5",
            },
        ],
        "related": ["review", "info"],
    },
    "series-comments": {
        "summary": "Get comments from all patches in a series",
        "description": """
Extract comments from every patch in a series in one call. Useful for
getting an overview of all feedback across the entire series.
""",
        "examples": [
            {
                "command": "gc series-comments URL",
                "description": "Get all unresolved comments in series",
            },
            {
                "command": "gc series-comments --all URL",
                "description": "Include resolved comments",
            },
            {
                "command": "gc series-comments --pretty URL",
                "description": "Human-readable output",
            },
        ],
        "related": ["comments", "review-series", "series-status"],
    },
    "series-status": {
        "summary": "Show status dashboard for a patch series",
        "description": """
Display a summary of all patches in a series: their status, comment counts,
review votes, and other metadata.
""",
        "examples": [
            {
                "command": "gc series-status URL",
                "description": "Show series status dashboard",
            },
        ],
        "related": ["review-series", "series-comments"],
    },
    "add-reviewer": {
        "summary": "Add a reviewer to a change",
        "description": """
Add a reviewer or CC to a Gerrit change. Supports fuzzy name matching -
just provide a partial name and it will find matches.

If multiple users match, you'll be shown the options and asked to be
more specific (use email or username for exact match).
""",
        "examples": [
            {
                "command": "gc add-reviewer URL \"John Smith\"",
                "description": "Add John Smith as reviewer (fuzzy match)",
            },
            {
                "command": "gc add-reviewer URL john@example.com",
                "description": "Add by email (exact match)",
            },
            {
                "command": "gc add-reviewer --cc URL jsmith",
                "description": "Add as CC instead of reviewer",
            },
        ],
        "related": ["remove-reviewer", "reviewers", "find-user"],
    },
    "remove-reviewer": {
        "summary": "Remove a reviewer from a change",
        "description": """
Remove a reviewer from a Gerrit change. Matches against current reviewers
by name, email, or username.
""",
        "examples": [
            {
                "command": "gc remove-reviewer URL \"John Smith\"",
                "description": "Remove John Smith from reviewers",
            },
            {
                "command": "gc remove-reviewer URL jsmith",
                "description": "Remove by username",
            },
        ],
        "related": ["add-reviewer", "reviewers"],
    },
    "reviewers": {
        "summary": "List reviewers and CCs on a change, with their activity",
        "description": """
Show everyone on a Gerrit change, reviewers and CCs alike, one entry each.

role is REVIEWER or CC, from Gerrit's own reviewer state; service_user
marks the accounts Gerrit tags as bots. is_bot marks every known bot,
those Gerrit leaves untagged (aireview, the RISC-V builder) included;
human_participant_count counts the entries that are not bots, and
people_besides_owner those that are neither bots nor the owner: the
count a cap on people on the change is checked against. owner marks
the change owner and commit_author the author of the current patchset's
commit (matched by email), who may be someone else, e.g. when a bot
uploads a person's commit. Both are also given at the top level: owner
and commit_author ({name, email}).
added_at and added_by say when and by whom each entered the role they
hold now. approvals is Gerrit's per-label score on the current patchset.

votes holds the non-zero votes standing on the current patchset.
copied_votes names the ones among them last cast on an earlier patchset
and carried forward by Gerrit; voted_on_current_patchset is true when at
least one was cast on the current one. last_vote is the latest message
in which the person set or removed a vote (a removal is a 0), with its
date and patchset.

last_activity is the person's latest change message or inline comment:
kind (message or comment), date, patchset, the text cut to 160
characters, file and line for a comment, and for a message its Gerrit tag
(autogenerated:gerrit:... for uploads, removals and the like, which are
not review). message_count and
comment_count count both. in_attention_set says whether Gerrit expects
an action from them.

removed lists people taken off the change, with removed_at, removed_by
and their last_activity. If the inline comments could not be read,
inline_comments_unavailable is true and last_activity comes from
messages alone. If the change detail cannot be read at all, only the
original fields (account_id, name, email, username, approvals) appear.
""",
        "examples": [
            {
                "command": "gc reviewers URL",
                "description": "List all reviewers and their votes",
            },
            {
                "command": "gc reviewers --pretty URL",
                "description": "Human-readable output",
            },
        ],
        "related": ["add-reviewer", "remove-reviewer"],
    },
    "reviewer-candidates": {
        "summary": "Rank reviewer candidates from the files' git history",
        "description": """
Who reviewed and who wrote the files a change touches, from a local
lustre-release checkout: per file, each person's Reviewed-by trailers and
authored commits (merges left out) over the window, default 12 months by
commit date. The change's files come from Gerrit (anonymous reads work);
--path gives files or directories instead, with or without a change.
History is read from --ref (default HEAD) in --repo (default the current
directory): use upstream/master when HEAD is an old or unrelated branch.

One person under two emails or two spellings of a name is merged, by
email and then by name (case, accents and punctuation ignored); aliases
lists what was merged. score is 2 x reviewed + authored (a commit counts
once however many of the files it touches); ties go to more files, then
the more recent. reviewed and authored count commits; last_reviewed and
last_authored are commit dates; files gives per-file counts, or bare
paths with --brief.
review_share is reviewed over commits_examined: near 1 means the person
is on almost every commit, as the gatekeeper who lands them is (Oleg
Drokin on lustre-release), so that count says little about these files.

The change's owner and its commit author are left out, and so are bots
(the same list as gerrit reviewers' is_bot) and anyone named with
--exclude; excluded lists them with a reason. People already on the
change stay in the ranking with on_change REVIEWER or CC (null if not
on it). Anonymous reads hide emails, so that match is by name.

paths_without_history names files with no commit in the window (new
files, or a repo that is not lustre-release): widen --months, or rank the
directory with --path. Picking who to add is still yours: check
MAINTAINERS and whether a candidate is active.
""",
        "examples": [
            {
                "command": "gc reviewer-candidates 12345 --repo ~/lustre-release --ref upstream/master",
                "description": "Rank candidates for a change's files",
            },
            {
                "command": "gc reviewer-candidates --path lustre/llite/file.c --months 24",
                "description": "Rank for one file over two years, no change",
            },
            {
                "command": "gc reviewer-candidates 12345 --brief --limit 5",
                "description": "Top five, files as bare paths",
            },
        ],
        "related": ["reviewers", "add-reviewer", "find-user"],
    },
    "search": {
        "summary": "Search for changes with Gerrit's query syntax",
        "description": """
The query goes to Gerrit as typed: every operator of the web UI search bar
works. One page by default (--limit, --start); --all follows the pages up
to --max. "more_results" is set when Gerrit says there are more, and
"next_start" is where the next page starts.

Operators that find related changes:
  message:"LU-12345"   commit message names it (a ticket, a sha, a word)
  comment:"TEXT"       a review comment mentions it, e.g. a change number
  path:"dir/file.c"    touches that file (exact; ^regex also works)
  file:NAME            touches a file with that path component
  topic:NAME           exact topic; hashtag:NAME (case-insensitive)
  parentof:N, is:open, status:merged, -is:abandoned, -change:N

after:/before: compare with the time a change was last MODIFIED, not
created -- there is no "created after" operator. For "changes since X",
after:X narrows the search on the server; check "created" yourself.
Results come newest-updated first, so a narrow query plus paging beats a
broad query with a small --limit.

Whether tr:/bug: work depends on the server's trackingid configuration,
and footer: only matches trailers written as Key: value it can parse --
if either returns nothing, fall back to message:.
""",
        "examples": [
            {
                "command": "gc search 'message:\"LU-12345\" after:2026-01-01 -is:abandoned'",
                "description": "Changes naming a ticket, touched since January",
            },
            {
                "command": "gc search --all 'path:\"lustre/llite/file.c\" after:2026-06-01 status:open'",
                "description": "Every open change touching a file, all pages",
            },
            {
                "command": "gc search 'comment:12345 -change:12345'",
                "description": "Changes whose reviews mention change 12345",
            },
        ],
        "related": ["related", "info"],
    },
    "find-user": {
        "summary": "Search for users by name",
        "description": """
Search for Gerrit users by name, email, or username. Useful for finding
the exact username before adding as a reviewer. is_bot marks the bot
accounts, by the same list as 'reviewers' (Gerrit leaves some untagged).
""",
        "examples": [
            {
                "command": "gc find-user \"John\"",
                "description": "Search for users named John",
            },
            {
                "command": "gc find-user --limit 20 \"smith\"",
                "description": "Get up to 20 results matching smith",
            },
        ],
        "related": ["add-reviewer", "reviewers"],
    },
    "batch": {
        "summary": "Reply to multiple comments from a JSON file",
        "description": """
Post multiple replies at once from a JSON file. The file should contain
an array of objects with thread_index, message, and optionally mark_resolved.

The replies go up as one message, as with 'push' (replies on several
patchsets refuse while you have other drafts on the change). It exits 0
only when every entry was posted: 1 when nothing was, 4 when an entry
matched no thread or comment (the others are still posted). Either way the error's
details carry posted, skipped_indices and the per-reply results.
""",
        "examples": [
            {
                "command": "gc batch URL replies.json",
                "description": "Post all replies from replies.json",
            },
        ],
        "related": ["reply", "stage", "push"],
    },
    "interactive": {
        "summary": "Interactive mode for reviewing comments",
        "description": """
Review and reply to comments in an interactive terminal interface.
""",
        "examples": [
            {
                "command": "gc interactive URL",
                "description": "Start interactive review mode",
            },
            {
                "command": "gc i URL",
                "description": "Short alias for interactive",
            },
        ],
        "related": ["comments", "review-series"],
    },
    "continue-reintegration": {
        "summary": "Continue reintegration after resolving conflicts",
        "description": """
After resolving merge conflicts during reintegration, use this command
to continue the cherry-pick process.
""",
        "examples": [
            {
                "command": "gc continue-reintegration",
                "description": "Continue after resolving conflicts",
            },
        ],
        "related": ["skip-reintegration", "finish-patch"],
    },
    "skip-reintegration": {
        "summary": "Skip current change during reintegration",
        "description": """
Skip a conflicting change during reintegration and move to the next one.
""",
        "examples": [
            {
                "command": "gc skip-reintegration",
                "description": "Skip current conflicting change",
            },
        ],
        "related": ["continue-reintegration", "finish-patch"],
    },
    "done": {
        "summary": "Resolve a comment thread with 'Done'",
        "description": """
Reply 'Done' (or your own text) to a thread and resolve it. A shortcut for
'gc reply --done'. The index is the one 'gc comments' printed.
""",
        "examples": [
            {
                "command": "gc done URL 0",
                "description": "Reply 'Done' to thread 0 and resolve it",
            },
            {
                "command": "gc done URL 2 \"Fixed in PS5\"",
                "description": "Resolve thread 2 with your own text",
            },
        ],
        "related": ["ack", "reply", "comments"],
    },
    "ack": {
        "summary": "Resolve a comment thread with 'Acknowledged'",
        "description": """
Reply 'Acknowledged' (or your own text) to a thread and resolve it. A
shortcut for 'gc reply --ack'. The index is the one 'gc comments' printed.
""",
        "examples": [
            {
                "command": "gc ack URL 1",
                "description": "Acknowledge thread 1 and resolve it",
            },
        ],
        "related": ["done", "reply", "comments"],
    },
    "info": {
        "summary": "Patchsets, votes and CI status of a change",
        "description": """
One call for the state of a change: subject, status, every patchset with
its upload date and uploader, each reviewer's votes, the enforced and
optional Maloo results and pending retests for the current patchset, and
the Jenkins build. Bot reviewers are left out unless --show-bots.

Maloo's vote is the verdict (ci.maloo_vote): a failure a retest has since
passed is not counted. A patchset that only changed the commit message is
not built or tested again; its results are the earlier patchset's, named
in ci.results_patchset.

mergeable is false when the current patchset no longer merges into its
branch, what the web UI shows as Merge Conflict: rebase the change and
upload a new patchset. It is true when it merges, and null for a closed
change or when Gerrit cannot say. Gerrit works it out on request, so it
costs one more request per open change.

For the text a reviewer posted with a vote, use
'gc comments URL --all --include-system'.
""",
        "examples": [
            {
                "command": "gc info 64086",
                "description": "Overview of change 64086",
            },
            {
                "command": "gc info --show-bots URL",
                "description": "Include bot reviewers",
            },
        ],
        "related": ["series-info", "maloo", "comments"],
    },
    "series-info": {
        "summary": "'gc info' for every patch in a series",
        "description": """
Find the series from any one of its patches and return 'gc info' for
each patch, root first: patchsets, votes, CI status and Jenkins build.
""",
        "examples": [
            {
                "command": "gc series-info URL",
                "description": "Info for every patch in the series",
            },
        ],
        "related": ["info", "series-status", "related"],
    },
    "maloo": {
        "summary": "Summarize Maloo CI results on a change",
        "description": """
Parse the Maloo messages on a change and summarize enforced and optional
test results, passed and failed, with their links. Several changes may be
given at once. --patchset picks a patchset other than the newest tested
one (single change only).

Maloo's vote is the verdict: once it is +1, earlier enforced failures show
as PASSED_LATER and are not counted. A patchset that only changed the
commit message is not tested again, so the results shown are the earlier
patchset's ("patchset" against "current_patchset").
""",
        "examples": [
            {
                "command": "gc maloo URL",
                "description": "CI results for the newest patchset",
            },
            {
                "command": "gc maloo -r 4 URL",
                "description": "CI results for patchset 4",
            },
            {
                "command": "gc maloo 64086 64087 64088",
                "description": "Several changes in one call",
            },
        ],
        "related": ["info", "watch"],
    },
    "watch": {
        "summary": "Run Maloo triage on a list of watched changes",
        "description": """
Read a JSON file holding an array of objects, each with at least a
'gerrit_url', and run the 'gc maloo' triage on every one.
""",
        "examples": [
            {
                "command": "gc watch ~/patches_to_watch.json",
                "description": "Triage every change in the file",
            },
        ],
        "related": ["maloo", "info"],
    },
    "vote": {
        "summary": "Set a label vote on a change",
        "description": """
Set a review label on the current patchset, such as Code-Review or
Verified, with an optional message. 'label' is an alias.
""",
        "examples": [
            {
                "command": "gc vote URL Code-Review +1",
                "description": "Vote Code-Review +1",
            },
            {
                "command": "gc vote URL Code-Review -1 -m \"See inline comments\"",
                "description": "Vote with a message",
            },
        ],
        "related": ["message", "review"],
    },
    "message": {
        "summary": "Post a change-level message",
        "description": """
Post a top-level message on a change, as the reply box of the Gerrit web
UI does: a patchset-level comment, not attached to a file or line, that
anyone can reply to. It is resolved; --unresolved opens it as a thread
that wants an answer. --no-thread posts a bare change message instead,
a change log entry nobody can reply to (for a notice). To vote at the
same time, use 'gc vote -m'.
""",
        "examples": [
            {
                "command": "gc message URL \"Rebased on the new master\"",
                "description": "Post a message on the change",
            },
            {
                "command": "gc message URL --unresolved \"Can the bot rerun sanity-sec?\"",
                "description": "Ask something that needs an answer",
            },
        ],
        "related": ["vote", "reply"],
    },
    "publish-drafts": {
        "summary": "Publish your draft comments on a change",
        "description": """
Publish every draft comment of the logged-in account on a change, on all
patchsets. Drafts mode exists to stop a publish, so the command refuses
in drafts mode. --dry-run lists the drafts in any mode.
--expect-patchset N refuses when the change is at another patchset.
""",
        "examples": [
            {
                "command": "gc publish-drafts URL --dry-run",
                "description": "List the drafts that a publish would post",
            },
            {
                "command": "gc publish-drafts URL --expect-patchset 3",
                "description": "Publish, only if the change is at patchset 3",
            },
        ],
        "related": ["message", "reply", "review"],
    },
    "abandon": {
        "summary": "Abandon a change",
        "description": """
Abandon a change, with an optional message given as a second argument or
with -m. --dry-run shows what would be abandoned and changes nothing.
""",
        "examples": [
            {
                "command": "gc abandon URL \"Superseded by 64100\"",
                "description": "Abandon with a message",
            },
            {
                "command": "gc abandon --dry-run URL",
                "description": "Show what would be abandoned",
            },
        ],
        "related": ["restore"],
    },
    "restore": {
        "summary": "Restore an abandoned change",
        "description": """
Bring an abandoned change back to open, with an optional message (-m).
""",
        "examples": [
            {
                "command": "gc restore URL -m \"Still needed\"",
                "description": "Restore with a message",
            },
        ],
        "related": ["abandon"],
    },
    "rebase": {
        "summary": "Rebase a change on the server",
        "description": """
Ask Gerrit to rebase a change onto its parent's newest patchset, or onto
the branch tip, without checking anything out. Gerrit refuses a rebase
that conflicts or has nothing to do.
""",
        "examples": [
            {
                "command": "gc rebase URL",
                "description": "Rebase the change on the server",
            },
        ],
        "related": ["related", "checkout", "upload"],
    },
    "set-topic": {
        "summary": "Set a change's topic",
        "description": """
Set or replace the topic of a change. Changes sharing a topic are grouped
together, and 'gc graph' follows them.
""",
        "examples": [
            {
                "command": "gc set-topic URL pcc-ro",
                "description": "Set the topic to pcc-ro",
            },
        ],
        "related": ["hashtag", "graph"],
    },
    "hashtag": {
        "summary": "List, add or remove a change's hashtags",
        "description": """
With no option, list the hashtags on a change. --add and --remove may be
repeated. Hashtags are free-form and can coexist with the topic.
""",
        "examples": [
            {
                "command": "gc hashtag URL",
                "description": "List the hashtags",
            },
            {
                "command": "gc hashtag URL --add ec --remove wip",
                "description": "Add one hashtag and remove another",
            },
        ],
        "related": ["set-topic", "graph"],
    },
    "related": {
        "summary": "The git relation chain of a change",
        "description": """
List every change in the relation chain of a change, root ancestor to tip,
in order: subject, status, Change-Id, the related patchset's commit and,
once merged, the commit that landed (merged_commit).
""",
        "examples": [
            {
                "command": "gc related URL",
                "description": "List the chain the change belongs to",
            },
        ],
        "related": ["series-info", "series-status", "graph"],
    },
    "checkout": {
        "summary": "Fetch a change and check it out",
        "description": """
Fetch a change's ref into the current git repository and check it out.
HEAD is detached unless --branch names a branch to create. --patchset
picks a patchset other than the newest. 'co' is an alias.
""",
        "examples": [
            {
                "command": "gc checkout URL",
                "description": "Check out the newest patchset, detached",
            },
            {
                "command": "gc checkout -r 3 -b review-64086 URL",
                "description": "Patchset 3 on a new branch",
            },
        ],
        "related": ["work-on-patch", "review"],
    },
    "graph": {
        "summary": "Interactive graph of every change related to a series",
        "description": """
Build an HTML graph of all the changes related to a series: branches,
abandoned forks and stale patchsets, plus the series sharing its topic
or hashtags. The argument may be a change or a JIRA ticket (LU-18222),
which picks the anchor change itself. Opens it in a browser unless
--no-open; -o saves it. --conflicts REPO trial-merges the in-flight
changes in a local clone. Unlike 'series-status', which follows one
linear chain, this shows the whole topology.
""",
        "examples": [
            {
                "command": "gc graph URL",
                "description": "Graph the series and open it",
            },
            {
                "command": "gc graph LU-18222 --no-open -o /tmp/lu18222.html",
                "description": "Graph a ticket's changes into a file",
            },
        ],
        "related": ["series-status", "related", "set-topic", "hashtag"],
    },
    "sashiko-review": {
        "summary": "Review a change with a Sashiko server",
        "description": """
Send a change to a running Sashiko instance for automated review, wait
for it (--timeout minutes), and post its findings as inline comments.
--dry-run shows the findings without posting; --vote adds a Code-Review
vote from their severity. 'sr' is an alias.
""",
        "examples": [
            {
                "command": "gc sashiko-review --dry-run 64591",
                "description": "Review and show findings only",
            },
        ],
        "related": ["review"],
    },
    "describe": {
        "summary": "Machine-readable description of the commands",
        "description": """
Print JSON describing the commands, their arguments, output fields and
suggested next actions. --command limits it to one command.
""",
        "examples": [
            {
                "command": "gc describe",
                "description": "Describe every command",
            },
            {
                "command": "gc describe --command comments",
                "description": "Describe one command",
            },
        ],
        "related": ["explain", "examples"],
    },
    "examples": {
        "summary": "Workflow cheatsheets",
        "description": """
Print example command sequences for a workflow: quick (the default),
staging, series, reviewers, or all.
""",
        "examples": [
            {
                "command": "gc examples staging",
                "description": "The staged-reply workflow",
            },
        ],
        "related": ["explain", "describe"],
    },
    "explain": {
        "summary": "Detailed usage and examples for one command",
        "description": """
Describe one command with examples and related commands. Aliases are
accepted. 'gc <command> --help' lists every argument.
""",
        "examples": [
            {
                "command": "gc explain comments",
                "description": "Explain the comments command",
            },
        ],
        "related": ["examples", "describe"],
    },
}

# Aliases for command lookup
COMMAND_ALIASES = {
    "extract": "comments",
    "i": "interactive",
    "co": "checkout",
    "s": "search",
    "label": "vote",
    "sr": "sashiko-review",
}


# Workflow examples for the 'examples' command
WORKFLOW_EXAMPLES = {
    "quick": {
        "title": "Quick Start - Single Change Review",
        "description": "The fastest way to review and reply to comments on a single change.",
        "examples": [
            ("gc comments URL", "Get unresolved comments (remembers URL)"),
            ("gc reply 0 --done", "Mark comment 0 as done (uses remembered URL)"),
            ("gc reply 1 \"Fixed in latest PS\"", "Reply to comment 1"),
        ],
    },
    "staging": {
        "title": "Staging Workflow - Batch Multiple Replies",
        "description": "Stage multiple replies locally, review them, then push all at once.",
        "examples": [
            ("gc comments URL", "Get comments to address"),
            ("gc stage --done 0", "Stage 'Done' for thread 0"),
            ("gc stage 1 \"Will fix in follow-up\"", "Stage reply for thread 1"),
            ("gc stage --ack 2", "Stage acknowledgment for thread 2"),
            ("gc upload CHANGE_ID", "Upload the amended commit as a new patchset"),
            ("gc staged list", "Review all staged replies"),
            ("gc push --dry-run CHANGE_ID", "Preview what will be posted"),
            ("gc push CHANGE_ID", "Post all staged replies"),
        ],
    },
    "series": {
        "title": "Series Workflow - Multi-Patch Review Session",
        "description": "Review a series of related patches interactively with rebase support.",
        "examples": [
            ("gc review-series URL", "Start session for a patch series"),
            ("gc status", "Check current session state"),
            ("gc comments", "Get comments for current patch"),
            ("gc stage --done 0", "Stage reply"),
            ("gc finish-patch", "Complete current patch, move to next"),
            ("gc push CHANGE_ID", "Push staged replies for a change"),
            ("gc abort", "Exit session without finishing"),
        ],
    },
    "reviewers": {
        "title": "Reviewer Management",
        "description": "Add, remove, and find reviewers on changes.",
        "examples": [
            ("gc reviewers URL", "Reviewers and CCs, with role, votes and last activity"),
            ("gc find-user john", "Search for users by name"),
            ("gc add-reviewer URL username", "Add a reviewer"),
            ("gc add-reviewer --cc URL username", "Add as CC only"),
            ("gc remove-reviewer URL username", "Remove a reviewer"),
        ],
    },
}


def cmd_search(args):
    """Search Gerrit for changes matching a query."""
    cli = _cli()
    command = "search"
    pretty = getattr(args, 'pretty', False)

    try:
        client = cli.GerritCommentsClient()
        if getattr(args, 'all', False):
            results = client.search_all(args.query, max_results=args.max)
        else:
            results = client.search_changes(
                query=args.query,
                limit=args.limit,
                start=args.start,
            )

        changes = []
        for change in results:
            owner = change.get("owner", {})
            entry = {
                "number": change.get("_number"),
                "subject": change.get("subject", ""),
                "project": change.get("project", ""),
                "branch": change.get("branch", ""),
                "status": change.get("status", ""),
                "owner": owner.get("name", owner.get("email", "")),
                "updated": change.get("updated", ""),
                "url": client.format_change_url(change.get("project", ""), change.get("_number")),
            }
            if change.get("created"):
                entry["created"] = change["created"]
            if change.get("submitted"):
                entry["submitted"] = change["submitted"]
            if change.get("topic"):
                entry["topic"] = change["topic"]
            if change.get("hashtags"):
                entry["hashtags"] = change["hashtags"]
            insertions = change.get("insertions", 0)
            deletions = change.get("deletions", 0)
            if insertions or deletions:
                entry["size"] = f"+{insertions}/-{deletions}"
            changes.append(entry)

        data = {
            "query": args.query,
            "count": len(changes),
            "changes": changes,
        }
        # Gerrit flags the last change of a page when there are more; a
        # full page alone does not mean that.
        if results and results[-1].get("_more_changes"):
            data["more_results"] = True
            start = 0 if getattr(args, 'all', False) else args.start
            data["next_start"] = start + len(results)

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_explain(args):
    """Show detailed usage for a specific command."""
    command_name = args.command_name.lower().replace("_", "-")

    # Resolve aliases
    command_name = COMMAND_ALIASES.get(command_name, command_name)

    if command_name not in COMMAND_EXPLANATIONS:
        available = sorted(COMMAND_EXPLANATIONS.keys())
        print(f"Unknown command: {command_name}", file=sys.stderr)
        print(f"\nAvailable commands:", file=sys.stderr)
        for cmd in available:
            info = COMMAND_EXPLANATIONS[cmd]
            print(f"  {cmd:20} {info['summary']}", file=sys.stderr)
        sys.exit(1)

    info = COMMAND_EXPLANATIONS[command_name]

    # Format output
    output = []
    output.append(f"gc {command_name} - {info['summary']}")
    output.append("=" * 60)
    output.append("")
    output.append("DESCRIPTION:")
    output.append(info['description'].strip())
    output.append("")
    output.append("EXAMPLES:")
    for ex in info['examples']:
        output.append(f"  $ {ex['command']}")
        output.append(f"    {ex['description']}")
        output.append("")

    if info.get('related'):
        output.append("RELATED COMMANDS:")
        output.append(f"  {', '.join(info['related'])}")
        output.append("")

    output.append(f"For full argument list, use: gc {command_name} --help")

    print("\n".join(output))


def cmd_examples(args):
    """Show common usage examples and workflows."""
    workflow = getattr(args, 'workflow', 'quick') or 'quick'

    if workflow == "all":
        # Show all workflows
        workflows_to_show = ["quick", "staging", "series", "reviewers"]
    else:
        workflows_to_show = [workflow]

    output = []
    output.append("=" * 60)
    output.append("GERRIT-COMMENTS EXAMPLES")
    output.append("=" * 60)
    output.append("")

    for wf_name in workflows_to_show:
        wf = WORKFLOW_EXAMPLES[wf_name]
        output.append(f"## {wf['title']}")
        output.append("")
        output.append(wf['description'])
        output.append("")

        for cmd, desc in wf['examples']:
            output.append(f"  $ {cmd}")
            output.append(f"    # {desc}")
        output.append("")

    output.append("-" * 60)
    output.append("Tips:")
    output.append("  - Use 'gc explain <command>' for detailed help on any command")
    output.append("  - URL can be a full Gerrit URL or just a change number")
    output.append("  - Most commands support --pretty for readable JSON output")
    output.append("")
    output.append("Workflows: quick, staging, series, reviewers, all")
    output.append("  $ gc examples staging    # Show staging workflow")
    output.append("  $ gc examples all        # Show all workflows")

    print("\n".join(output))


def cmd_describe(args):
    """Show machine-readable API description."""
    from ..cli import build_parser
    from ..describe import complete_description, get_tool_description

    pretty = getattr(args, 'pretty', False)
    command_name = getattr(args, 'command_name', None)
    tool_desc = complete_description(get_tool_description(), build_parser())

    if command_name:
        normalized = command_name.replace(".", " ")
        normalized = COMMAND_ALIASES.get(normalized, normalized)
        matching = [c for c in tool_desc.commands if c.name == normalized]
        if not matching:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"Unknown command: {command_name}",
                "describe",
                pretty,
            ))
        data = matching[0].to_dict()
    else:
        data = tool_desc.to_dict()

    output_success(data, "describe", pretty)
    sys.exit(ExitCode.SUCCESS)
