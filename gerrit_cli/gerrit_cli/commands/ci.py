"""CI/testing commands: maloo, info, watch, diff."""

import sys

from ..errors import ErrorCode, ExitCode
from ._helpers import BOT_REVIEWER_NAMES, _cli, _patchset_age, output_error, output_success


def _maloo_for_change(client, change_number, patchset=None):
    """Get Maloo CI results for a single change. Returns a dict."""
    change = client.get_change_detail(change_number)
    msgs = client.get_messages(change_number)

    if not patchset:
        patchset = max(
            (m.get('_revision_number', 0) for m in msgs),
            default=0,
        )

    # Get patchset upload date from revisions
    patchset_uploaded = None
    for rev_id, rev in change.get("revisions", {}).items():
        if rev.get("_number") == patchset:
            patchset_uploaded = rev.get("created")
            break

    enforced_results = {}
    optional_failed = []
    retests = []

    for m in msgs:
        author = m.get('author', {}).get('name', '')
        ps = m.get('_revision_number', 0)
        text = m.get('message', '')

        if author == 'Autotest' and ps == patchset:
            if 'retest' in text.lower():
                retests.append({
                    'date': m.get('date', ''),
                    'message': text.strip()[:150],
                })

        if author != 'Maloo':
            continue
        if ps != patchset:
            continue

        for kind in ('enforced', 'optional'):
            for status in ('Failed', 'Passed'):
                marker = f'{status} {kind} test '
                if marker not in text:
                    continue
                rest = text.split(marker, 1)[1]
                name_plat = rest.split(' uploaded')[0].strip()
                parts = name_plat.split(' on ', 1)
                test_name = parts[0].strip()
                platform = parts[1].strip() if len(parts) > 1 else ''
                url = ''
                test_detail = ''
                if 'https://testing.' in text:
                    after_marker = text[text.index('https://testing.'):]
                    url = after_marker.split()[0]
                    after_url = after_marker[len(url):].strip()
                    if after_url:
                        test_detail = after_url

                if kind == 'enforced':
                    if test_name not in enforced_results:
                        enforced_results[test_name] = {'pass': [], 'fail': []}
                    bucket = 'pass' if status == 'Passed' else 'fail'
                    entry = {'platform': platform, 'url': url}
                    if test_detail:
                        entry['detail'] = test_detail
                    enforced_results[test_name][bucket].append(entry)
                elif status == 'Failed':
                    entry = {'test': test_name, 'platform': platform, 'url': url}
                    if test_detail:
                        entry['detail'] = test_detail
                    optional_failed.append(entry)

    retested_groups = set()
    for rt in retests:
        msg = rt['message'].lower()
        for test_name in enforced_results:
            if test_name.lower() in msg:
                retested_groups.add(test_name)

    enforced_summary = []
    total_pass = 0
    total_fail = 0
    for test_name in sorted(enforced_results):
        r = enforced_results[test_name]
        p = len(r['pass'])
        f = len(r['fail'])
        total_pass += p
        total_fail += f
        if f and p:
            verdict = 'MIXED'
        elif f:
            verdict = 'FAIL'
        else:
            verdict = 'PASS'
        entry = {
            'test': test_name, 'verdict': verdict,
            'passed': p, 'failed': f,
        }
        if r['fail']:
            entry['failures'] = r['fail']
        if test_name in retested_groups:
            entry['retest_pending'] = True
        enforced_summary.append(entry)

    data = {
        'change_number': change_number,
        'subject': change.get('subject', ''),
        'patchset': patchset,
        'patchset_uploaded': patchset_uploaded,
        'patchset_age': _patchset_age(patchset_uploaded) if patchset_uploaded else None,
        'enforced': {
            'total_pass': total_pass,
            'total_fail': total_fail,
            'tests': enforced_summary,
        },
        'optional_failures': optional_failed,
    }

    if retests:
        data['retests'] = retests

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
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_info(args):
    """Show quick overview of a change: patchsets, reviews, CI status."""
    cli = _cli()
    command = "info"
    pretty = getattr(args, 'pretty', False)
    show_bots = getattr(args, 'show_bots', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        # Get change detail (includes ALL_REVISIONS)
        change = client.get_change_detail(change_number)

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

        # Get reviewers with approvals, filtering bots by default
        reviewers_raw = client.get_reviewers(change_number)
        reviewers = []
        for r in reviewers_raw:
            name = r.get("name", "")
            approvals = r.get("approvals", {})
            if not approvals:
                continue
            if not show_bots and name in BOT_REVIEWER_NAMES:
                continue
            reviewers.append({
                "name": name,
                "approvals": approvals,
            })

        # Get CI status via maloo message parsing (reuse cmd_maloo logic)
        msgs = client.get_messages(change_number)

        enforced_results = {}
        optional_failed = []
        retests_pending = []

        for m in msgs:
            author = m.get('author', {}).get('name', '')
            ps = m.get('_revision_number', 0)
            text = m.get('message', '')

            if author == 'Maloo' and ps == current_patchset:
                for kind in ('enforced', 'optional'):
                    for status in ('Failed', 'Passed'):
                        marker = f'{status} {kind} test '
                        if marker not in text:
                            continue
                        rest = text.split(marker, 1)[1]
                        name_plat = rest.split(' uploaded')[0].strip()
                        parts = name_plat.split(' on ', 1)
                        test_name = parts[0].strip()
                        platform = parts[1].strip() if len(parts) > 1 else ''
                        url = ''
                        if 'https://testing.' in text:
                            after_marker = text[text.index('https://testing.'):]
                            url = after_marker.split()[0]

                        if kind == 'enforced':
                            if test_name not in enforced_results:
                                enforced_results[test_name] = {'pass': 0, 'fail': 0}
                            enforced_results[test_name]['pass' if status == 'Passed' else 'fail'] += 1
                        elif status == 'Failed':
                            optional_failed.append(test_name)

            # Track retests
            if author == 'Autotest' and ps == current_patchset:
                if 'retest' in text.lower():
                    retests_pending.append({
                        'date': m.get('date', ''),
                        'message': text[:120],
                    })

        # Summarize CI
        enforced_pass = sum(r['pass'] for r in enforced_results.values())
        enforced_fail = sum(r['fail'] for r in enforced_results.values())

        ci_status = "no results"
        if enforced_pass or enforced_fail:
            if enforced_fail == 0:
                ci_status = "all passing"
            elif enforced_pass == 0:
                ci_status = "failing"
            else:
                ci_status = "mixed"

        data = {
            "change_number": change_number,
            "project": change.get("project", ""),
            "branch": change.get("branch", ""),
            "subject": change.get("subject", ""),
            "status": change.get("status", ""),
            "owner": change.get("owner", {}).get("name", ""),
            "current_patchset": current_patchset,
            "current_revision": current_revision,
            "patchsets": patchsets,
            "reviewers": reviewers,
            "ci": {
                "status": ci_status,
                "enforced_pass": enforced_pass,
                "enforced_fail": enforced_fail,
                "optional_fail": len(optional_failed),
                "retests_pending": len(retests_pending),
            },
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


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
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, False))


def cmd_diff(args):
    """Show what changed between two patchsets."""
    cli = _cli()
    command = "diff"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        patchset_a = args.patchset_a
        patchset_b = args.patchset_b

        # If patchset_b not specified, use latest
        if patchset_b is None:
            change = client.get_change_detail(change_number)
            revisions = change.get("revisions", {})
            patchset_b = max(
                (r.get("_number", 1) for r in revisions.values()),
                default=1,
            )

        if patchset_a >= patchset_b:
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
                    str(patchset_a), str(patchset_b),
                )
                # Format diff content from the API response
                lines = []
                for section in diff_data.get("content", []):
                    if "ab" in section:
                        # Common lines (context)
                        for line in section["ab"]:
                            lines.append(f" {line}")
                    if "a" in section:
                        for line in section["a"]:
                            lines.append(f"-{line}")
                    if "b" in section:
                        for line in section["b"]:
                            lines.append(f"+{line}")

                diffs.append({
                    "path": f["path"],
                    "status": f["status"],
                    "lines_inserted": f["lines_inserted"],
                    "lines_deleted": f["lines_deleted"],
                    "diff": "\n".join(lines),
                })
            except Exception:
                diffs.append({
                    "path": f["path"],
                    "status": f["status"],
                    "lines_inserted": f["lines_inserted"],
                    "lines_deleted": f["lines_deleted"],
                    "diff": "(diff unavailable)",
                })

        data = {
            "change_number": change_number,
            "patchset_a": patchset_a,
            "patchset_b": patchset_b,
            "files_changed": len(file_list),
            "files": diffs,
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))
