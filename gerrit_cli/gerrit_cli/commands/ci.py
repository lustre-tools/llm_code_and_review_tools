"""CI/testing commands: maloo, info, watch, diff."""

import sys

from .. import ci_parse
from ..errors import ErrorCode, ExitCode
from ._helpers import (
    BOT_REVIEWER_NAMES,
    _cli,
    _patchset_age,
    error_code_for,
    output_error,
    output_success,
)

# One GET carries everything `info` and `maloo` read: the revisions and
# their kinds, the bot messages, and every vote on every label.
CHANGE_OPTIONS = [
    "ALL_REVISIONS",
    "CURRENT_COMMIT",
    "DETAILED_ACCOUNTS",
    "DETAILED_LABELS",
    "MESSAGES",
]


CI_STATUS = {
    "PASS": "all passing",
    "FAIL": "failing",
    "RUNNING": "running",
    "NONE": "no results",
}


def _standing_failures(tests):
    """Enforced failures Maloo still counts against the change.

    A failure a later pass on the same platform cleared is already gone
    from failed_tests.  A Maloo +1 clears the rest: a retest that passes
    often gets no message of its own, only Maloo's new vote.
    """
    if tests["maloo_vote"] > 0:
        return 0
    return sum(len(t["failures"]) for t in tests["failed_tests"])


def _maloo_for_change(client, change_number, patchset=None):
    """Get Maloo CI results for a single change. Returns a dict."""
    change = client.get_change(change_number, CHANGE_OPTIONS)
    current = ci_parse.current_ps(change)
    # A commit-message-only patchset is not built or tested again: its
    # results, and the votes Gerrit copied to it, are an earlier one's.
    patchset = patchset or ci_parse.ci_relevant_ps(change)
    tests = ci_parse.parse_tests(change, patchset)

    patchset_uploaded = None
    for rev in change.get("revisions", {}).values():
        if rev.get("_number") == patchset:
            patchset_uploaded = rev.get("created")
            break

    passed_later = tests["maloo_vote"] > 0
    enforced = []
    for t in tests["failed_tests"]:
        entry = {
            "test": t["name"],
            "verdict": "PASSED_LATER" if passed_later else "FAIL",
            "failed": len(t["failures"]),
            "failures": t["failures"],
        }
        if t["retest_pending"]:
            entry["retest_pending"] = True
        enforced.append(entry)

    data = {
        "change_number": change_number,
        "subject": change.get("subject", ""),
        "patchset": patchset,
        "current_patchset": current,
        "patchset_uploaded": patchset_uploaded,
        "patchset_age": _patchset_age(patchset_uploaded) if patchset_uploaded else None,
        "state": tests["state"],
        "maloo_vote": tests["maloo_vote"],
        "enforced": {
            "total_fail": _standing_failures(tests),
            "tests": enforced,
        },
        "optional_failures": tests["optional_failures"],
    }
    if tests["retests"]:
        data["retests"] = tests["retests"]
    return data


def cmd_maloo(args):
    """Triage Maloo test results from a Gerrit change."""
    cli = _cli()
    command = "maloo"
    pretty = getattr(args, 'pretty', False)

    try:
        urls = args.url  # Now a list (nargs='+')
        client = cli.GerritCommentsClient()
        patchset = getattr(args, 'patchset', None)

        if len(urls) == 1:
            # Single URL mode - same output format as before
            base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(urls[0])
            data = _maloo_for_change(client, change_number, patchset)
            output_success(data, command, pretty)
        else:
            # Batch mode - array of results
            if patchset:
                sys.exit(output_error(
                    ErrorCode.INVALID_INPUT,
                    "--patchset not supported in batch mode",
                    command, pretty))

            results = []
            for url in urls:
                try:
                    _, change_number = cli.GerritCommentsClient.parse_gerrit_url(url)
                    result = _maloo_for_change(client, change_number)
                    results.append(result)
                except Exception as e:
                    results.append({
                        'url': url,
                        'error': str(e),
                    })

            output_success({"changes": results, "count": len(results)},
                           command, pretty)

        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def _has_negative_vote(approvals):
    """True if any label carries a negative vote.

    Gerrit reports approvals as strings -- " 0", "+1", "-1", "-2" -- so a
    vote is negative exactly when its stripped form starts with "-".
    """
    return any(str(value).strip().startswith("-") for value in approvals.values())


def _info_for_change(client, change_number, show_bots=False):
    """Get info data for a single change. Returns a dict.

    Extracts patchset history, reviewers, CI status, and Jenkins build
    for the given change number. Used by both cmd_info and cmd_series_info.
    """
    change = client.get_change(change_number, CHANGE_OPTIONS)

    # Build patchset info with upload dates and age
    revisions = change.get("revisions", {})
    patchsets = []
    for rev_id, rev in revisions.items():
        created = rev.get("created", "")
        ps_entry = {
            "number": rev.get("_number"),
            "created": created,
            "uploader": rev.get("uploader", {}).get("name", ""),
        }
        age = _patchset_age(created)
        if age:
            ps_entry["age"] = age
        patchsets.append(ps_entry)
    patchsets.sort(key=lambda x: x["number"])

    current_revision = change.get("current_revision", "")
    current_patchset = revisions.get(current_revision, {}).get("_number", 0)

    # Get reviewers with approvals, filtering bots by default -- but never a
    # bot's NEGATIVE vote.  Hiding "Looks good to me" chatter is the point of
    # the filter; hiding wc-checkpatch's standing -1 ("cannot be cherry-picked
    # to master, please rebase") reported a vetoed change as clean, and the
    # only way to see the veto was a flag nobody following the documented
    # workflow would think to pass.
    reviewers_raw = client.get_reviewers(change_number)
    msgs = change.get("messages") or []
    # The date of the newest review message from each account. A vote
    # or a comment makes a message, so this is the last activity.
    last_activity = {}
    for m in msgs:
        author = m.get('author', {})
        key = author.get('_account_id', author.get('name'))
        last_activity[key] = max(last_activity.get(key, ''),
                                 m.get('date', ''))

    reviewers = []
    for r in reviewers_raw:
        name = r.get("name", "")
        approvals = r.get("approvals", {})
        if not approvals:
            continue
        if (
            not show_bots
            and name in BOT_REVIEWER_NAMES
            and not _has_negative_vote(approvals)
        ):
            continue
        entry = {
            "name": name,
            "approvals": approvals,
        }
        if r.get("username"):
            entry["username"] = r["username"]
        entry["last_activity"] = last_activity.get(
            r.get("_account_id", name), "")
        reviewers.append(entry)

    results_ps = ci_parse.ci_relevant_ps(change)
    tests = ci_parse.parse_tests(change)
    build = ci_parse.parse_build(change)

    ci = {
        "status": CI_STATUS[tests["state"]],
        "maloo_vote": tests["maloo_vote"],
        "enforced_fail": _standing_failures(tests),
        "optional_fail": tests["optional_fail"],
        "retests_pending": sum(1 for t in tests["failed_tests"] if t["retest_pending"]),
    }
    if results_ps != current_patchset:
        ci["results_patchset"] = results_ps

    jenkins_build = None
    if build["state"] != "NONE":
        jenkins_build = {
            "status": build["state"],
            "build_url": build["url"],
            "build_number": build["number"],
        }

    # Whether the current patchset still merges into its branch: false is
    # the web UI's "Merge Conflict", a change to rebase.  Gerrit computes
    # it on request and leaves it out of the change, so ask.  Only an open
    # change can conflict; null when it is closed or Gerrit cannot say.
    mergeable = None
    if change.get("status") == "NEW" and current_revision:
        try:
            answer = client.get_mergeable(change_number, current_revision)
        except Exception:
            answer = {}
        if isinstance(answer, dict) and isinstance(answer.get("mergeable"), bool):
            mergeable = answer["mergeable"]

    return {
        "change_number": change_number,
        "change_id": change.get("change_id", ""),
        "project": change.get("project", ""),
        "branch": change.get("branch", ""),
        "subject": change.get("subject", ""),
        "status": change.get("status", ""),
        "mergeable": mergeable,
        "owner": change.get("owner", {}).get("name", ""),
        "current_patchset": current_patchset,
        "current_revision": current_revision,
        "patchsets": patchsets,
        "reviewers": reviewers,
        "ci": ci,
        "jenkins_build": jenkins_build,
    }


def cmd_info(args):
    """Show quick overview of a change: patchsets, reviews, CI status."""
    cli = _cli()
    command = "info"
    pretty = getattr(args, 'pretty', False)
    show_bots = getattr(args, 'show_bots', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()
        data = _info_for_change(client, change_number, show_bots)
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_series_info(args):
    """Show info for all patches in a series: patchsets, reviews, CI status."""
    cli = _cli()
    command = "series-info"
    pretty = getattr(args, 'pretty', False)
    show_bots = getattr(args, 'show_bots', False)

    try:
        from ..parallel import parallel_map
        from ..series import SeriesFinder

        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        # Discover the series
        finder = SeriesFinder(client=client)
        series = finder.find_series(args.url)

        if not series or not series.patches:
            output_success({
                "series": {"total_patches": 0, "patches": []},
                "patches": [],
            }, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        # Get info for each patch in the series
        def info_for(patch):
            try:
                return _info_for_change(client, patch.change_number, show_bots)
            except Exception as e:
                return {
                    "change_number": patch.change_number,
                    "subject": patch.subject,
                    "error": str(e),
                }

        patches_info = parallel_map(info_for, series.patches)

        data = {
            "series": series.to_dict(),
            "patches": patches_info,
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_watch(args):
    """Check CI status on a list of watched patches from a JSON file."""
    import json as _json
    cli = _cli()
    command = "watch"

    try:
        with open(args.file) as f:
            raw = _json.load(f)

        # Support both bare array and {patches: [...]} object formats
        if isinstance(raw, list):
            patches = raw
        elif isinstance(raw, dict) and "patches" in raw:
            patches = raw["patches"]
        else:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                "JSON file must contain an array or {patches: [...]}",
                command, False))

        client = cli.GerritCommentsClient()
        results = []

        for patch in patches:
            gerrit_url = patch.get("gerrit_url")
            if not gerrit_url:
                results.append({
                    "error": "Missing gerrit_url field",
                    "entry": patch,
                })
                continue

            try:
                _, change_number = cli.GerritCommentsClient.parse_gerrit_url(
                    gerrit_url)
                data = _maloo_for_change(client, change_number)
                # Merge extra fields from the watch entry
                if patch.get("jira"):
                    data["jira"] = patch["jira"]
                if patch.get("description"):
                    data["description"] = patch["description"]
                if patch.get("notes"):
                    data["notes"] = patch["notes"]
                data["gerrit_url"] = gerrit_url
                results.append(data)
            except Exception as e:
                results.append({
                    "gerrit_url": gerrit_url,
                    "error": str(e),
                })

        output_success({"patches": results, "count": len(results)},
                       command, False)
        sys.exit(ExitCode.SUCCESS)

    except FileNotFoundError:
        sys.exit(output_error(
            ErrorCode.INVALID_INPUT,
            f"File not found: {args.file}", command, False))
    except _json.JSONDecodeError as e:
        sys.exit(output_error(
            ErrorCode.INVALID_INPUT,
            f"Invalid JSON: {e}", command, False))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, False))


def cmd_diff(args):
    """Show what changed between two patchsets.

    With no patchsets given, the newest patchset against the one before
    it; a change with one patchset is shown against its parent commit.
    """
    from ..reviewer import content_to_hunks
    cli = _cli()
    command = "diff"
    pretty = getattr(args, 'pretty', False)
    context_lines = getattr(args, 'unified', 3)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        patchset_a = args.patchset_a
        patchset_b = args.patchset_b

        if patchset_b is None:
            change = client.get_change_detail(change_number)
            revisions = change.get("revisions", {})
            patchset_b = max(
                (r.get("_number", 1) for r in revisions.values()),
                default=1,
            )

        note = None
        if patchset_a is None:
            patchset_a = patchset_b - 1
            if patchset_a < 1:
                patchset_a = None
                note = (f"patchset {patchset_b} has no patchset before it, "
                        "so this is the change against its parent commit, "
                        "as gc review shows it")
        elif patchset_a >= patchset_b:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"Base patchset ({patchset_a}) must be less than "
                f"target patchset ({patchset_b})",
                command, pretty
            ))

        # Get list of changed files between patchsets
        files = client.get_files_between_patchsets(
            change_number, patchset_a, patchset_b,
        )

        # Build file summary (skip /COMMIT_MSG and /MERGE_LIST)
        file_list = []
        for path, info in files.items():
            if path.startswith("/"):
                continue
            file_list.append({
                "path": path,
                "status": info.get("status", "M"),
                "lines_inserted": info.get("lines_inserted", 0),
                "lines_deleted": info.get("lines_deleted", 0),
            })

        # Get diffs for each changed file
        diffs = []
        for f in file_list:
            try:
                diff_data = client.get_diff(
                    change_number, f["path"],
                    str(patchset_a) if patchset_a is not None else None,
                    str(patchset_b),
                )
                hunks = content_to_hunks(
                    diff_data.get("content", []), context_lines)
                diff_text = "\n".join(h.format() for h in hunks)
            except Exception:
                diff_text = "(diff unavailable)"
            diffs.append({**f, "diff": diff_text})

        data = {
            "change_number": change_number,
            "patchset_a": patchset_a,
            "patchset_b": patchset_b,
            "files_changed": len(file_list),
            "files": diffs,
        }
        if note:
            data["note"] = note

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
