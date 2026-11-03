"""Change management commands: abandon, restore, rebase, vote, set-topic,
message, publish-drafts."""

import sys

from ..errors import ErrorCode, ExitCode
from ..mode import (MODE_ENV_VAR, MODE_FULL, DraftModeError, get_mode,
                    result_fields)
from ._helpers import _cli, error_code_for, output_error, output_success


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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


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
            thread=getattr(args, 'thread', True),
        )

        data = {
            "success": True,
            "change_number": change_number,
            "label": args.label,
            "score": args.score,
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except DraftModeError as e:
        sys.exit(output_error(ErrorCode.CONFIG_ERROR, str(e), command, pretty))
    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_related(args):
    """Get the relation chain (series) for a Gerrit change."""
    cli = _cli()
    command = "related"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        changes = client.get_related_changes(change_number)

        merged = [c["_change_number"] for c in changes
                  if c.get("_change_number") is not None
                  and c.get("status") == "MERGED"]
        landed = client.get_current_revisions(merged) if merged else {}

        # Build clean output: list of change numbers in series order
        series = []
        for c in changes:
            num = c.get("_change_number")
            if num is None:
                continue
            commit = c.get("commit") or {}
            status = c.get("status", "NEW")
            series.append({
                "change_number": num,
                "patchset": c.get("_revision_number"),
                "current_patchset": c.get("_current_revision_number"),
                "status": status,
                "subject": commit.get("subject") or c.get("subject", ""),
                "is_current": num == change_number,
                "change_id": c.get("change_id"),
                "commit": commit.get("commit"),
                "merged_commit": (landed.get(num)
                                  if status == "MERGED" else None),
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
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_message(args):
    """Post a top-level message on a Gerrit change."""
    cli = _cli()
    command = "message"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        unresolved = getattr(args, 'unresolved', False)
        thread = getattr(args, 'thread', True)
        if unresolved and not thread:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                "--unresolved needs a thread; it cannot go with --no-thread",
                command, pretty))
        client.post_review(
            change_number=change_number,
            revision_id="current",
            message=args.text,
            unresolved=unresolved,
            thread=thread,
        )

        data = {
            "success": True,
            "change_number": change_number,
            "message": args.text,
            "unresolved": unresolved,
            "thread": thread,
            **result_fields(),
        }
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


def cmd_publish_drafts(args):
    """Publish the draft comments of the logged-in account on a change.

    Drafts mode exists to stop a publish, so the command refuses unless
    the mode is full. --dry-run lists the drafts in any mode.
    --expect-patchset refuses when the change is at another patchset.
    """
    cli = _cli()
    command = "publish-drafts"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

        info = client.get_change(change_number, ["CURRENT_REVISION"])
        current = info.get("current_revision_number") or \
            (info.get("revisions") or {}).get(
                info.get("current_revision"), {}).get("_number")
        expect = getattr(args, 'expect_patchset', None)
        if expect is not None and current != expect:
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"change {change_number} is at patchset {current}, "
                f"not {expect}", command, pretty))

        drafts = [
            {"path": path, "line": d.get("line"),
             "patch_set": d.get("patch_set"), "id": d.get("id"),
             "in_reply_to": d.get("in_reply_to"),
             "unresolved": d.get("unresolved"),
             "message": d.get("message", "")}
            for path, items in (client.get_drafts(change_number) or {}).items()
            for d in items
        ]
        data = {
            "change_number": change_number,
            "current_patchset": current,
            "drafts": drafts,
            "count": len(drafts),
            **result_fields(),
        }

        if getattr(args, 'dry_run', False):
            data.update(dry_run=True, published=False,
                        would_publish=len(drafts),
                        mode_allows_publish=get_mode() == MODE_FULL)
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        if get_mode() != MODE_FULL:
            sys.exit(output_error(
                ErrorCode.CONFIG_ERROR,
                f"{MODE_ENV_VAR} is not '{MODE_FULL}': drafts mode publishes "
                f"nothing. Publish in the Gerrit web UI, or have the "
                f"account owner run this command in full mode.", command,
                pretty))
        if not drafts:
            data.update(success=True, published=False)
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        client.publish_drafts(change_number)
        data.update(success=True, published=True)
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))


# Gerrit's own text for a deleted comment or change message
_REMOVED = ("Comment removed by:", "Change message removed by:")


def _delete_refusal(command, pretty):
    """Exit when a delete cannot or should not be sent."""
    if get_mode() != MODE_FULL:
        sys.exit(output_error(
            ErrorCode.CONFIG_ERROR,
            f"{MODE_ENV_VAR} is not '{MODE_FULL}': a deletion cannot be "
            "drafted or undone. Have the account owner run it in full mode.",
            command, pretty))


def _delete_error(e, command, pretty):
    message = str(e)
    if "403" in message or "Forbidden" in message:
        message += (" -- deleting needs Gerrit's Administrate Server "
                    "capability, which this account does not have")
    sys.exit(output_error(error_code_for(e), message, command, pretty))


def cmd_delete_comment(args):
    """Delete a published comment (Gerrit admins only).

    Gerrit keeps the comment in its thread but replaces its text with
    "Comment removed by: <name>; Reason: <reason>" and resolves it.
    """
    cli = _cli()
    command = "delete-comment"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()
        if not getattr(args, 'dry_run', False):
            _delete_refusal(command, pretty)

        found = [
            (path, c) for path, items in (client.get_comments(change_number) or {}).items()
            for c in items if c.get("id") == args.comment_id
        ]
        if not found:
            sys.exit(output_error(
                ErrorCode.NOT_FOUND,
                f"Change {change_number} has no published comment "
                f"{args.comment_id}. Comment ids are in 'gc comments <url> "
                "--all' (root_comment.id, replies[].id); a draft is not "
                "deleted this way.", command, pretty))
        path, comment = found[0]
        if comment.get("message", "").startswith(_REMOVED):
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"Comment {args.comment_id} is already deleted: "
                f"{comment['message']}", command, pretty))

        data = {
            "change_number": change_number,
            "comment_id": args.comment_id,
            "path": path,
            "line": comment.get("line"),
            "patch_set": comment.get("patch_set"),
            "author": (comment.get("author") or {}).get("name"),
            "message": comment.get("message", ""),
            "reason": args.reason,
        }
        if getattr(args, 'dry_run', False):
            data.update(dry_run=True, deleted=False)
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        result = client.delete_comment(
            change_number, str(comment.get("patch_set")), args.comment_id,
            args.reason)
        data.update(deleted=True, new_message=(result or {}).get("message"))
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        _delete_error(e, command, pretty)


def cmd_delete_message(args):
    """Delete a change message (Gerrit admins only).

    Gerrit replaces its text with "Change message removed by: <name>"
    and the reason. The message and every later one get new ids.
    """
    cli = _cli()
    command = "delete-message"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()
        if not getattr(args, 'dry_run', False):
            _delete_refusal(command, pretty)

        wanted = args.message_id.lower()
        found = [m for m in client.get_messages(change_number) or []
                 if m.get("id", "").startswith(wanted)]
        if len(found) != 1:
            sys.exit(output_error(
                ErrorCode.NOT_FOUND if not found else ErrorCode.INVALID_INPUT,
                (f"Change {change_number} has no change message "
                 f"{args.message_id}. Message ids change whenever a comment "
                 "or message on the change is deleted; list them again "
                 "with 'gc comments <url> --messages'."
                 if not found else
                 f"{args.message_id} matches {len(found)} change messages; "
                 "give more of the id."), command, pretty))
        message = found[0]
        if message.get("message", "").startswith(_REMOVED):
            sys.exit(output_error(
                ErrorCode.INVALID_INPUT,
                f"Change message {message['id']} is already deleted: "
                f"{message['message']}", command, pretty))

        data = {
            "change_number": change_number,
            "message_id": message["id"],
            "date": message.get("date"),
            "patch_set": message.get("_revision_number"),
            "author": (message.get("author") or {}).get("name"),
            "message": message.get("message", ""),
            "reason": args.reason,
        }
        if getattr(args, 'dry_run', False):
            data.update(dry_run=True, deleted=False)
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        result = client.delete_message(change_number, message["id"], args.reason)
        data.update(deleted=True, new_message_id=(result or {}).get("id"),
                    new_message=(result or {}).get("message"))
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        _delete_error(e, command, pretty)
