"""Comment commands: comments/extract, reply, batch-reply, done, ack."""

import sys

from ..errors import ErrorCode, ExitCode
from ..session import LastURLManager
from ..summary import truncate_extracted_comments
from ._helpers import _cli, filter_threads_by_fields, output_error, output_success


def cmd_extract(args):
    """Extract comments from a Gerrit change."""
    cli = _cli()
    command = "extract"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)
    fields = getattr(args, 'fields', None)

    try:
        include_system = getattr(args, 'include_system', False)
        include_ci = getattr(args, 'include_ci', False)
        # --include-system implies --include-ci (system messages without
        # CI bot messages is rarely useful and leads to confusion)
        if include_system:
            include_ci = True
        result = cli.extract_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
            include_system=include_system,
            exclude_ci_bots=not include_ci,
        )

        if fields:
            # Output filtered flat list of threads (--fields takes precedence)
            data = {
                "threads": filter_threads_by_fields(result.threads, fields),
                "count": len(result.threads),
            }
        else:
            data = result.to_dict()
            if summary_lines is not None:
                data = truncate_extracted_comments(data, summary_lines)

        # Save URL for subsequent commands (gc reply without URL)
        LastURLManager().save(args.url)

        output_success(
            data, command, pretty,
            next_actions=[
                "gc reply <INDEX> \"<message>\" -- reply to a thread",
                "gc reply --done <INDEX> -- mark a thread as done",
                "gc stage --done <INDEX> -- stage a 'done' reply for later",
                "gc review <URL> -- view code diffs",
            ],
        )
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error extracting comments: {e}", command, pretty))


def cmd_reply(args):
    """Reply to a comment."""
    cli = _cli()
    command = "reply"
    pretty = getattr(args, 'pretty', False)

    # Get URL from args or last-used
    url = getattr(args, 'url', None)
    if not url:
        url = LastURLManager().load()
        if not url:
            sys.exit(output_error(
                ErrorCode.MISSING_REQUIRED_FIELD,
                "No URL provided and no recent URL found. Run 'gc comments URL' first or use --url.",
                command, pretty
            ))

    try:
        # Parse URL to get change number
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(url)

        # Extract to get the threads
        result = cli.extract_comments(
            url=url,
            include_resolved=False,
            include_code_context=False,
        )

        if args.thread_index >= len(result.threads):
            if len(result.threads) == 0:
                msg = (
                    "No unresolved comment threads on this change. "
                    "To post a top-level review comment use: gc message <URL> \"<text>\""
                )
            else:
                msg = f"Thread index {args.thread_index} out of range. Only {len(result.threads)} threads."
            sys.exit(output_error(
                ErrorCode.THREAD_INDEX_OUT_OF_RANGE,
                msg,
                command, pretty
            ))

        thread = result.threads[args.thread_index]

        # Determine message and resolved status
        if args.done:
            message = args.message or "Done"
            mark_resolved = True
        elif args.ack:
            message = args.message or "Acknowledged"
            mark_resolved = True
        else:
            message = args.message
            mark_resolved = args.resolve

        if not message:
            sys.exit(output_error(
                ErrorCode.MISSING_REQUIRED_FIELD,
                "Message is required (or use --done/--ack)",
                command, pretty
            ))

        # Handle dry-run mode
        dry_run = getattr(args, 'dry_run', False)
        if dry_run:
            last_comment = thread.replies[-1] if thread.replies else thread.root_comment
            data = {
                "dry_run": True,
                "would_post": {
                    "change_number": change_number,
                    "thread_index": args.thread_index,
                    "file": last_comment.file_path,
                    "line": last_comment.line,
                    "message": message,
                    "mark_resolved": mark_resolved,
                },
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        # Post the reply
        replier = cli.CommentReplier()
        reply_result = replier.reply_to_thread(
            change_number=change_number,
            thread=thread,
            message=message,
            mark_resolved=mark_resolved,
        )

        if reply_result.success:
            output_success(reply_result.to_dict(), command, pretty)
            sys.exit(ExitCode.SUCCESS)
        else:
            sys.exit(output_error(ErrorCode.API_ERROR, reply_result.error or "Unknown error", command, pretty))

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error posting reply: {e}", command, pretty))


def cmd_done(args):
    """Mark a comment as done (shortcut for reply --done)."""
    cli = _cli()
    command = "done"
    pretty = getattr(args, 'pretty', False)

    try:
        # Parse URL to get change number
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get the threads
        result = cli.extract_comments(
            url=args.url,
            include_resolved=False,
            include_code_context=False,
        )

        if args.thread_index >= len(result.threads):
            sys.exit(output_error(
                ErrorCode.THREAD_INDEX_OUT_OF_RANGE,
                f"Thread index {args.thread_index} out of range. Only {len(result.threads)} threads.",
                command, pretty
            ))

        thread = result.threads[args.thread_index]
        message = args.message or "Done"

        # Post the reply
        replier = cli.CommentReplier()
        reply_result = replier.reply_to_thread(
            change_number=change_number,
            thread=thread,
            message=message,
            mark_resolved=True,
        )

        if reply_result.success:
            output_success(reply_result.to_dict(), command, pretty)
            sys.exit(ExitCode.SUCCESS)
        else:
            sys.exit(output_error(ErrorCode.API_ERROR, reply_result.error or "Unknown error", command, pretty))

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error marking comment done: {e}", command, pretty))


def cmd_ack(args):
    """Acknowledge a comment (shortcut for reply --ack)."""
    cli = _cli()
    command = "ack"
    pretty = getattr(args, 'pretty', False)

    try:
        # Parse URL to get change number
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get the threads
        result = cli.extract_comments(
            url=args.url,
            include_resolved=False,
            include_code_context=False,
        )

        if args.thread_index >= len(result.threads):
            sys.exit(output_error(
                ErrorCode.THREAD_INDEX_OUT_OF_RANGE,
                f"Thread index {args.thread_index} out of range. Only {len(result.threads)} threads.",
                command, pretty
            ))

        thread = result.threads[args.thread_index]
        message = args.message or "Acknowledged"

        # Post the reply
        replier = cli.CommentReplier()
        reply_result = replier.reply_to_thread(
            change_number=change_number,
            thread=thread,
            message=message,
            mark_resolved=True,
        )

        if reply_result.success:
            output_success(reply_result.to_dict(), command, pretty)
            sys.exit(ExitCode.SUCCESS)
        else:
            sys.exit(output_error(ErrorCode.API_ERROR, reply_result.error or "Unknown error", command, pretty))

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error acknowledging comment: {e}", command, pretty))


def cmd_batch_reply(args):
    """Reply to multiple comments from a JSON file."""
    import json as json_module
    cli = _cli()
    command = "batch-reply"
    pretty = getattr(args, 'pretty', False)

    try:
        # Load replies from JSON file
        with open(args.file) as f:
            replies_data = json_module.load(f)

        # Parse URL
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get threads
        result = cli.extract_comments(
            url=args.url,
            include_resolved=False,
            include_code_context=False,
        )

        # Build reply list
        replies = []
        skipped = []
        for item in replies_data:
            thread_idx = item['thread_index']
            if thread_idx >= len(result.threads):
                skipped.append(thread_idx)
                continue

            thread = result.threads[thread_idx]
            last_comment = thread.replies[-1] if thread.replies else thread.root_comment

            replies.append({
                'comment': last_comment,
                'message': item['message'],
                'mark_resolved': item.get('mark_resolved', False),
                'thread_index': thread_idx,
            })

        # Handle dry-run mode
        dry_run = getattr(args, 'dry_run', False)
        if dry_run:
            would_post = []
            for reply_spec in replies:
                comment = reply_spec['comment']
                would_post.append({
                    "thread_index": reply_spec['thread_index'],
                    "file": comment.file_path,
                    "line": comment.line,
                    "message": reply_spec['message'],
                    "mark_resolved": reply_spec['mark_resolved'],
                })
            data = {
                "dry_run": True,
                "change_number": change_number,
                "would_post": would_post,
                "total": len(would_post),
                "skipped_indices": skipped,
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        # Post all replies
        replier = cli.CommentReplier()
        results = replier.batch_reply(change_number=change_number, replies=replies)

        # Build result data
        success_count = sum(1 for r in results if r.success)
        data = {
            "posted": success_count,
            "total": len(results),
            "skipped_indices": skipped,
            "results": [r.to_dict() for r in results],
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))
