"""Reviewer management commands: reviewers, add-reviewer, remove-reviewer, find-user."""

import re
import sys

from ..ci_parse import is_bot_participant
from ..errors import ErrorCode, ExitCode
from ..parallel import parallel_map
from ._helpers import _cli, error_code_for, output_error, output_success


def reviewer_since(updates):
    """{account_id: (added_at, added_by)}: when each account last entered the
    state it is in now, REVIEWER or CC, and who put it there."""
    current = {}
    for update in sorted(updates, key=lambda item: str(item.get("updated") or "")):
        account = (update.get("reviewer") or {}).get("_account_id")
        state = update.get("state")
        if account is None:
            continue
        if state == "REMOVED":
            current.pop(account, None)
        elif current.get(account, (None,))[0] != state:
            by = update.get("updated_by") or {}
            current[account] = (
                state, str(update.get("updated") or ""),
                by.get("username") or by.get("name") or by.get("_account_id"),
            )
    return {account: (since, by) for account, (_state, since, by) in current.items()}


# One call for roles, dates, votes and messages.
_DETAIL_OPTIONS = [
    "DETAILED_LABELS", "DETAILED_ACCOUNTS", "REVIEWER_UPDATES", "MESSAGES",
    "CURRENT_REVISION", "CURRENT_COMMIT",
]
_VOTE_HEADER = re.compile(r"^(?:Patch Set|Uploaded patch set) \d+:(.*?)\.?$")
_SNIPPET = 160


def message_votes(text, labels):
    """{label: value} a change message's first line cast; 0 is a removal."""
    match = _VOTE_HEADER.match((text or "").split("\n", 1)[0])
    votes = {}
    for token in (match.group(1).split() if match else ()):
        cast = re.fullmatch(r"([A-Za-z][\w-]*?)([+-]\d+)", token)
        if cast and cast.group(1) in labels:
            votes[cast.group(1)] = int(cast.group(2))
            continue
        removed = re.fullmatch(r"-([A-Za-z][\w-]*)", token)
        if removed and removed.group(1) in labels:
            votes[removed.group(1)] = 0
    return votes


def _snippet(text):
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= _SNIPPET else flat[:_SNIPPET - 3] + "..."


def _person(account):
    return {
        "account_id": account.get("_account_id"),
        "name": account.get("name", ""),
        "email": account.get("email", ""),
        "username": account.get("username", ""),
    }


def reviewer_activity(change, comments):
    """{account_id: fields}: each account's role, standing votes, whether
    they were cast on the current patchset, and its latest message or
    inline comment on the change.

    Gerrit's timestamps are UTC in one fixed format, so the latest is the
    greatest string.
    """
    labels = change.get("labels") or {}
    revisions = change.get("revisions") or {}
    current = (revisions.get(change.get("current_revision") or "") or {}).get("_number")
    owner = (change.get("owner") or {}).get("_account_id")
    attention = {str(key) for key in (change.get("attention_set") or {})}

    people = {}

    def entry(account_id):
        return people.setdefault(account_id, {
            "role": None, "votes": {}, "last_cast": {}, "last_vote": None,
            "last_activity": None, "message_count": 0, "comment_count": 0,
        })

    for role in ("REVIEWER", "CC"):
        for account in (change.get("reviewers") or {}).get(role) or []:
            entry(account.get("_account_id"))["role"] = role

    for label, info in labels.items():
        for vote in (info or {}).get("all") or []:
            if vote.get("value") not in (None, 0):
                entry(vote.get("_account_id"))["votes"][label] = vote["value"]

    def note(person, activity):
        latest = person["last_activity"]
        if latest is None or activity["date"] >= latest["date"]:
            person["last_activity"] = activity

    for message in change.get("messages") or []:
        account = (message.get("author") or {}).get("_account_id")
        if account is None:
            continue
        person = entry(account)
        date = str(message.get("date") or "")
        patchset = message.get("_revision_number")
        person["message_count"] += 1
        note(person, {"kind": "message", "date": date, "patchset": patchset,
                      "tag": message.get("tag"),
                      "text": _snippet(message.get("message"))})
        cast = message_votes(message.get("message"), labels)
        if cast:
            for label in cast:
                person["last_cast"][label] = patchset
            if person["last_vote"] is None or date >= person["last_vote"]["date"]:
                person["last_vote"] = {"date": date, "patchset": patchset,
                                       "labels": cast}

    for path, thread in (comments or {}).items():
        for comment in thread or []:
            account = (comment.get("author") or {}).get("_account_id")
            if account is None:
                continue
            person = entry(account)
            person["comment_count"] += 1
            note(person, {"kind": "comment",
                          "date": str(comment.get("updated") or ""),
                          "patchset": comment.get("patch_set"),
                          "file": path, "line": comment.get("line"),
                          "text": _snippet(comment.get("message"))})

    found = {}
    for account, person in people.items():
        last_cast = person.pop("last_cast")
        copied = sorted(label for label in person["votes"]
                        if last_cast.get(label, current) != current)
        found[account] = dict(
            person,
            owner=account == owner,
            in_attention_set=str(account) in attention,
            has_current_vote=bool(person["votes"]),
            copied_votes=copied,
            voted_on_current_patchset=len(copied) < len(person["votes"]),
        )
    return current, found


def reviewer_removals(updates):
    """{account_id: (removed_at, removed_by, account)} for each account whose
    last reviewer update took it off the change."""
    last = {}
    for update in sorted(updates, key=lambda item: str(item.get("updated") or "")):
        account = (update.get("reviewer") or {}).get("_account_id")
        if account is not None:
            last[account] = update
    removed = {}
    for account, update in last.items():
        if update.get("state") == "REMOVED":
            by = update.get("updated_by") or {}
            removed[account] = (
                str(update.get("updated") or ""),
                by.get("username") or by.get("name") or by.get("_account_id"),
                update.get("reviewer") or {},
            )
    return removed


def commit_author(change):
    """{name, email} of the current patchset's commit author, or None.

    The author need not own the change (a bot can upload another's
    commit), and Gerrit names the author by email only.
    """
    revisions = change.get("revisions") or {}
    commit = (revisions.get(change.get("current_revision") or "") or {}).get("commit") or {}
    author = commit.get("author") or {}
    if not author.get("email"):
        return None
    return {"name": author.get("name", ""), "email": author["email"]}


def cmd_reviewers(args):
    """List reviewers and CCs on a change, with role, votes and activity."""
    cli = _cli()
    command = "reviewers"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()
        reviewers = client.get_reviewers(change_number)
        try:
            change = client.get_change(change_number, options=_DETAIL_OPTIONS)
        except Exception:
            change = None
        comments, comments_ok = {}, True
        if change is not None:
            try:
                comments = client.get_comments(change_number) or {}
            except Exception:
                comments_ok = False
        if change is not None:
            updates = change.get("reviewer_updates") or []
            joined = reviewer_since(updates)
            current, activity = reviewer_activity(change, comments)
        else:
            joined, current, activity = None, None, {}

        author = commit_author(change) if change is not None else None
        author_email = (author or {}).get("email", "").lower()
        owner_id = ((change or {}).get("owner") or {}).get("_account_id")

        reviewer_list = []
        for r in reviewers:
            reviewer_info = dict(_person(r), approvals=r.get("approvals", {}))
            # Tag, name or username: aireview and the RISC-V builder are
            # bots without the SERVICE_USER tag, and Patrickbot is an agent.
            reviewer_info["is_bot"] = is_bot_participant(r)
            if change is not None:
                account = r.get("_account_id")
                added_at, added_by = joined.get(account, (None, None))
                reviewer_info["added_at"] = added_at
                reviewer_info["added_by"] = added_by
                reviewer_info["service_user"] = "SERVICE_USER" in (r.get("tags") or [])
                reviewer_info.update(activity.get(account) or {
                    "role": None, "votes": {}, "last_vote": None,
                    "last_activity": None, "message_count": 0,
                    "comment_count": 0, "owner": False,
                    "in_attention_set": False, "has_current_vote": False,
                    "copied_votes": [], "voted_on_current_patchset": False,
                })
                reviewer_info["owner"] = account == owner_id
                reviewer_info["commit_author"] = bool(
                    author_email
                    and (r.get("email") or "").lower() == author_email)
            reviewer_list.append(reviewer_info)

        data = {
            "change_number": change_number,
            "reviewers": reviewer_list,
            "count": len(reviewer_list),
        }
        if change is not None:
            data["current_patchset"] = current
            data["reviewer_count"] = sum(
                1 for r in reviewer_list if r["role"] == "REVIEWER")
            data["cc_count"] = sum(1 for r in reviewer_list if r["role"] == "CC")
            # Everyone listed who is not a bot: the people already on the
            # change, owner and commit author included when listed.
            data["human_participant_count"] = sum(
                1 for r in reviewer_list if not r["is_bot"])
            # The count a reviewer cap is checked against: people, both
            # roles, not the owner (67507's run counted these by hand).
            data["people_besides_owner"] = sum(
                1 for r in reviewer_list if not r["is_bot"] and not r["owner"])
            data["owner"] = _person((change or {}).get("owner") or {})
            data["commit_author"] = author
            listed = {r["account_id"] for r in reviewer_list}
            data["removed"] = [
                dict(_person(account), removed_at=at, removed_by=by,
                     last_activity=(activity.get(account_id) or {}).get("last_activity"))
                for account_id, (at, by, account)
                in reviewer_removals(change.get("reviewer_updates") or []).items()
                if account_id not in listed
            ]
            if not comments_ok:
                data["inline_comments_unavailable"] = True

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def _match_account(client, name):
    """(account, None) for the one account *name* means, or (None, why):
    "none" or the list of fuzzy matches to choose from."""
    matches = client.suggest_accounts(name, limit=5)
    if not matches:
        # Try a broader search
        matches = client.search_accounts(name=name, limit=5)
    if not matches:
        return None, "none"
    # An exact username or email wins over fuzzy neighbours
    # ("LiX" also suggests "Lixin ..." accounts).
    wanted = name.strip().lower()
    exact = [m for m in matches
             if wanted and wanted in ((m.get("username") or "").lower(),
                                      (m.get("email") or "").lower())]
    if len(matches) == 1:
        return matches[0], None
    if len(exact) == 1:
        return exact[0], None
    return None, [{"name": m.get("name", ""), "email": m.get("email", ""),
                   "username": m.get("username", "")} for m in matches]


def _who(account):
    return {"name": account.get("name", ""), "email": account.get("email", ""),
            "username": account.get("username", "")}


def _outcome(result, state):
    """What Gerrit says it did: added as reviewer or cc, already there
    (it answers an add of someone present with nothing), or an error."""
    if not isinstance(result, dict):
        return {"result": "added_" + state.lower()}
    if result.get("error"):
        return {"result": "error", "error": result["error"]}
    if result.get("reviewers"):
        return {"result": "added_reviewer"}
    if result.get("ccs"):
        return {"result": "added_cc"}
    return {"result": "already_on_change"}


def cmd_add_reviewer(args):
    """Add one or more reviewers to a change, with fuzzy name matching.

    Every name is matched before anyone is added, so a name that is
    ambiguous or unknown adds nobody.  The adds run together, and each
    reports Gerrit's own answer, so no `gc reviewers` is needed after.
    """
    cli = _cli()
    command = "add-reviewer"
    pretty = getattr(args, 'pretty', False)
    names = getattr(args, "names", None) or [args.name]

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        found = parallel_map(lambda n: _match_account(client, n), names)
        unknown = [n for n, (_, why) in zip(names, found) if why == "none"]
        ambiguous = {n: why for n, (_, why) in zip(names, found)
                     if isinstance(why, list)}
        if unknown and not ambiguous:
            error_msg = (f"No users found matching {', '.join(repr(n) for n in unknown)}. "
                         "Nobody was added. Try a different spelling or use "
                         "'gc find-user' to search.")
            sys.exit(output_error(ErrorCode.NOT_FOUND, error_msg, command, pretty))
        if ambiguous:
            data = {
                "error": "multiple_matches",
                "message": ("Multiple users match "
                            + ", ".join(repr(n) for n in ambiguous)
                            + ". Nobody was added; be more specific."),
                "matches": (next(iter(ambiguous.values()))
                            if len(ambiguous) == 1 else ambiguous),
                "hint": "Use email or username for exact match, e.g.: gc add-reviewer URL user@example.com",
            }
            if unknown:
                data["not_found"] = unknown
            output_success(data, command, pretty)
            sys.exit(ExitCode.GENERAL_ERROR)

        accounts = [account for account, _ in found]
        state = "CC" if args.cc else "REVIEWER"

        # Handle dry-run mode
        if getattr(args, 'dry_run', False):
            data = {
                "dry_run": True,
                "change_number": change_number,
                "would_add": [{**_who(a), "state": state} for a in accounts],
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        def add(account):
            reviewer_id = (account.get("username") or account.get("email")
                           or str(account.get("_account_id")))
            try:
                return client.add_reviewer(change_number, reviewer_id, state=state)
            except Exception as e:  # one failure must not hide the others
                return e

        results = parallel_map(add, accounts)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors and len(errors) == len(results):
            raise errors[0]  # reported as the command's error, below
        added = [
            {**_who(a), "state": state,
             **({"result": "error", "error": str(r)[:300]}
                if isinstance(r, Exception) else _outcome(r, state))}
            for a, r in zip(accounts, results)
        ]
        failed = any(a["result"] == "error" for a in added)
        data = {
            "success": not failed,
            "change_number": change_number,
            "added": added,
        }
        output_success(data, command, pretty)
        sys.exit(ExitCode.GENERAL_ERROR if failed else ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        error_str = str(e)
        # Provide better error messages for common cases
        if "404" in error_str or "not found" in error_str.lower():
            sys.exit(output_error(
                ErrorCode.NOT_FOUND,
                f"Change {change_number} not found or you don't have access",
                command, pretty
            ))
        elif "403" in error_str or "forbidden" in error_str.lower():
            sys.exit(output_error(
                ErrorCode.AUTH_FAILED,
                "Permission denied - you may not have rights to add reviewers",
                command, pretty
            ))
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_remove_reviewer(args):
    """Remove a reviewer from a change."""
    cli = _cli()
    command = "remove-reviewer"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        # Get current reviewers to find the one to remove
        reviewers = client.get_reviewers(change_number)

        # Find matching reviewer
        name_lower = args.name.lower()
        matched = None
        for r in reviewers:
            if (name_lower in r.get("name", "").lower() or
                name_lower in r.get("email", "").lower() or
                name_lower == r.get("username", "").lower()):
                matched = r
                break

        if not matched:
            current_reviewers = [
                f"{r.get('name', '')} ({r.get('username', '')})"
                for r in reviewers
            ]
            error_msg = f"No reviewer matching '{args.name}' found on this change. "
            if current_reviewers:
                error_msg += f"Current reviewers: {', '.join(current_reviewers)}"
            else:
                error_msg += "This change has no reviewers."
            sys.exit(output_error(ErrorCode.NOT_FOUND, error_msg, command, pretty))

        # Remove the reviewer
        reviewer_id = matched.get("username") or matched.get("email") or str(matched.get("_account_id"))

        # Handle dry-run mode
        dry_run = getattr(args, 'dry_run', False)
        if dry_run:
            data = {
                "dry_run": True,
                "change_number": change_number,
                "would_remove": {
                    "name": matched.get("name", ""),
                    "email": matched.get("email", ""),
                    "username": matched.get("username", ""),
                },
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        client.remove_reviewer(change_number, reviewer_id)

        data = {
            "success": True,
            "change_number": change_number,
            "removed": {
                "name": matched.get("name", ""),
                "email": matched.get("email", ""),
                "username": matched.get("username", ""),
            },
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def _find_user(client, query, limit):
    # Use suggest for fuzzy matching
    matches = client.suggest_accounts(query, limit=limit)
    if not matches:
        # Try a broader search
        matches = client.search_accounts(name=query, limit=limit)
    user_list = [{
        "name": m.get("name", ""),
        "email": m.get("email", ""),
        "username": m.get("username", ""),
        "account_id": m.get("_account_id"),
        # Same bot list as 'reviewers' and 'reviewer-candidates':
        # aireview and the RISC-V builder lack Gerrit's tag.
        "is_bot": is_bot_participant(m),
    } for m in matches or []]
    data = {"query": query, "users": user_list, "count": len(user_list)}
    if not user_list:
        data["message"] = f"No users found matching '{query}'"
    return data


def cmd_find_user(args):
    """Search for users by name; several queries are looked up together."""
    cli = _cli()
    command = "find-user"
    pretty = getattr(args, 'pretty', False)
    queries = getattr(args, "queries", None) or [args.query]

    try:
        client = cli.GerritCommentsClient()
        found = parallel_map(
            lambda q: _find_user(client, q, args.limit), queries)
        data = found[0] if len(found) == 1 else {
            "results": found, "count": len(found)}
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
