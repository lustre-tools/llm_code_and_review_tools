"""Reviewer management commands: reviewers, add-reviewer, remove-reviewer, find-user."""

import sys

from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, output_error, output_success


def cmd_reviewers(args):
    """List reviewers on a change."""
    cli = _cli()
    command = "reviewers"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()
        reviewers = client.get_reviewers(change_number)

        # Format reviewer data
        reviewer_list = []
        for r in reviewers:
            reviewer_info = {
                "account_id": r.get("_account_id"),
                "name": r.get("name", ""),
                "email": r.get("email", ""),
                "username": r.get("username", ""),
                "approvals": r.get("approvals", {}),
            }
            reviewer_list.append(reviewer_info)

        data = {
            "change_number": change_number,
            "reviewers": reviewer_list,
            "count": len(reviewer_list),
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_add_reviewer(args):
    """Add a reviewer to a change with fuzzy name matching."""
    cli = _cli()
    command = "add-reviewer"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        # First, try to find matching users
        matches = client.suggest_accounts(args.name, limit=5)

        if not matches:
            # Try a broader search
            matches = client.search_accounts(name=args.name, limit=5)

        if not matches:
            error_msg = f"No users found matching '{args.name}'. "
            error_msg += "Try a different spelling or use 'gc find-user' to search."
            sys.exit(output_error(ErrorCode.NOT_FOUND, error_msg, command, pretty))

        # If exactly one match, use it directly
        if len(matches) == 1:
            selected = matches[0]
        else:
            # Multiple matches - show them and ask user to be more specific
            match_list = []
            for m in matches:
                match_list.append({
                    "name": m.get("name", ""),
                    "email": m.get("email", ""),
                    "username": m.get("username", ""),
                })

            data = {
                "error": "multiple_matches",
                "message": f"Multiple users match '{args.name}'. Please be more specific.",
                "matches": match_list,
                "hint": "Use email or username for exact match, e.g.: gc add-reviewer URL user@example.com",
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.GENERAL_ERROR)

        # Add the reviewer
        reviewer_id = selected.get("username") or selected.get("email") or str(selected.get("_account_id"))
        state = "CC" if args.cc else "REVIEWER"

        # Handle dry-run mode
        dry_run = getattr(args, 'dry_run', False)
        if dry_run:
            data = {
                "dry_run": True,
                "change_number": change_number,
                "would_add": {
                    "name": selected.get("name", ""),
                    "email": selected.get("email", ""),
                    "username": selected.get("username", ""),
                    "state": state,
                },
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        result = client.add_reviewer(change_number, reviewer_id, state=state)

        data = {
            "success": True,
            "change_number": change_number,
            "added": {
                "name": selected.get("name", ""),
                "email": selected.get("email", ""),
                "username": selected.get("username", ""),
                "state": state,
            },
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

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
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


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
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_find_user(args):
    """Search for users by name."""
    cli = _cli()
    command = "find-user"
    pretty = getattr(args, 'pretty', False)

    try:
        client = cli.GerritCommentsClient()

        # Use suggest for fuzzy matching
        matches = client.suggest_accounts(args.query, limit=args.limit)

        if not matches:
            # Try a broader search
            matches = client.search_accounts(name=args.query, limit=args.limit)

        user_list = []
        for m in matches:
            user_list.append({
                "name": m.get("name", ""),
                "email": m.get("email", ""),
                "username": m.get("username", ""),
                "account_id": m.get("_account_id"),
            })

        data = {
            "query": args.query,
            "users": user_list,
            "count": len(user_list),
        }

        if not user_list:
            data["message"] = f"No users found matching '{args.query}'"

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))
