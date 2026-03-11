"""Change management commands: abandon, restore, rebase, vote, set-topic, message."""

import sys

from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, output_error, output_success


def cmd_abandon(args):
    """Abandon a Gerrit change."""
    cli = _cli()
    command = "abandon"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        # --message/-m flag takes precedence over positional
        message = getattr(args, 'message_flag', None) or getattr(args, 'message', '') or ''

        # Handle dry-run mode
        dry_run = getattr(args, 'dry_run', False)
        if dry_run:
            data = {
                "dry_run": True,
                "change_number": change_number,
                "would_abandon": True,
                "message": message or "(no message)",
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        result = client.abandon_change(change_number, message=message)

        data = {
            "success": True,
            "change_number": change_number,
            "status": result.get("status", "ABANDONED"),
            "subject": result.get("subject", ""),
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        error_str = str(e)
        if "409" in error_str:
            sys.exit(output_error(
                ErrorCode.API_ERROR,
                f"Change {change_number} cannot be abandoned (may already be abandoned or merged)",
                command, pretty
            ))
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_restore(args):
    """Restore an abandoned Gerrit change."""
    cli = _cli()
    command = "restore"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        message = getattr(args, 'message', '') or ''
        result = client.restore_change(change_number, message=message)

        data = {
            "success": True,
            "change_number": change_number,
            "status": result.get("status", "NEW"),
            "subject": result.get("subject", ""),
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        error_str = str(e)
        if "409" in error_str:
            sys.exit(output_error(
                ErrorCode.API_ERROR,
                f"Change {change_number} cannot be restored "
                f"(may not be abandoned)",
                command, pretty
            ))
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_rebase(args):
    """Trigger a server-side rebase of a Gerrit change."""
    cli = _cli()
    command = "rebase"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        result = client.rebase_change(change_number)

        data = {
            "success": True,
            "change_number": change_number,
            "status": result.get("status", "NEW"),
            "subject": result.get("subject", ""),
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        error_str = str(e)
        if "409" in error_str:
            sys.exit(output_error(
                ErrorCode.API_ERROR,
                f"Change {change_number} cannot be rebased "
                f"(may have merge conflicts or already be up to date)",
                command, pretty
            ))
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_vote(args):
    """Set a review label/vote on a Gerrit change."""
    cli = _cli()
    command = "vote"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        message = getattr(args, 'message', '') or ''
        labels = {args.label: args.score}

        client.post_review(
            change_number=change_number,
            revision_id="current",
            message=message or None,
            labels=labels,
        )

        data = {
            "success": True,
            "change_number": change_number,
            "label": args.label,
            "score": args.score,
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_set_topic(args):
    """Set the topic on a Gerrit change."""
    cli = _cli()
    command = "set-topic"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        result = client.set_topic(change_number, args.topic)

        data = {
            "success": True,
            "change_number": change_number,
            "topic": args.topic,
        }
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_hashtag(args):
    """Get or modify hashtags on a Gerrit change."""
    cli = _cli()
    command = "hashtag"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        add = getattr(args, 'add', None) or []
        remove = getattr(args, 'remove', None) or []

        if add or remove:
            hashtags = client.add_hashtags(change_number, add=add, remove=remove or None)
            data = {
                "success": True,
                "change_number": change_number,
                "hashtags": hashtags,
            }
        else:
            hashtags = client.get_hashtags(change_number)
            data = {
                "change_number": change_number,
                "hashtags": hashtags,
            }

        output_success(data, command, pretty)
        sys.exit(0)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_related(args):
    """Get the relation chain (series) for a Gerrit change."""
    cli = _cli()
    command = "related"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        changes = client.get_related_changes(change_number)

        # Build clean output: list of change numbers in series order
        series = []
        for c in changes:
            num = c.get("_change_number")
            if num is None:
                continue
            series.append({
                "change_number": num,
                "patchset": c.get("_revision_number"),
                "current_patchset": c.get("_current_revision_number"),
                "status": c.get("status", "NEW"),
                "subject": c.get("subject", ""),
                "is_current": num == change_number,
            })

        data = {
            "change_number": change_number,
            "series_length": len(series),
            "series": series,
        }

        output_success(data, command, pretty)
        sys.exit(0)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_message(args):
    """Post a top-level message on a Gerrit change."""
    cli = _cli()
    command = "message"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        client.post_review(
            change_number=change_number,
            revision_id="current",
            message=args.text,
        )

        data = {
            "success": True,
            "change_number": change_number,
            "message": args.text,
        }
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))
