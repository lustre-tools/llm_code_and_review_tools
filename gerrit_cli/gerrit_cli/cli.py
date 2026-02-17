#!/usr/bin/env python3
"""Command-line interface for Gerrit comments tools.

This CLI provides commands for:
1. comments - Get unresolved comments from a Gerrit change
2. reply - Reply to comments or mark them as done
3. review - Get diff/changes for code review, optionally post review comments

All commands output JSON.

Examples:
    # Get unresolved comments
    gc comments https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Reply to a comment (by thread index from comments output)
    gc reply https://review.whamcloud.com/c/fs/lustre-release/+/62796 0 "Done"

    # Mark a comment as done
    gc reply --done https://review.whamcloud.com/c/fs/lustre-release/+/62796 0

    # Get changes for code review
    gc review https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Post a code review with comments from JSON file
    gc review --post-comments comments.json https://review.whamcloud.com/62796
"""

import argparse
import sys
from typing import Any

from .client import GerritCommentsClient
from .envelope import error_response_from_dict, format_json, success_response
from .errors import ErrorCode, ExitCode
from .extractor import extract_comments
from .interactive import run_interactive
from .interactive_vim import run_interactive_vim
from .rebase import (
    RebaseManager,
    abort_patch,
    end_session,
    finish_patch,
    get_session_info,
    get_session_url,
    next_patch,
    rebase_status,
    work_on_patch,
)
from .replier import CommentReplier
from .reviewer import CodeReviewer
from .series import SeriesFinder
from .session import LastURLManager
from .series_status import show_series_status
from .staging import StagingManager
from .summary import truncate_extracted_comments, truncate_review_data, truncate_series_comments

# Bot account names to filter from reviewer lists
BOT_REVIEWER_NAMES: set[str] = {
    "Maloo", "jenkins", "Jenkins", "Autotest",
    "wc-checkpatch", "Lustre Gerrit Janitor",
    "Misc Code Checks Robot (Gatekeeper helper)",
    "CI Bot", "Build Bot", "Janitor Bot",
}


def _patchset_age(timestamp_str: str) -> str:
    """Convert a Gerrit timestamp to a human-readable age string."""
    from datetime import datetime, timezone
    try:
        # Gerrit format: "2026-02-18 17:35:19.000000000"
        ts = timestamp_str.split(".")[0]
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining_m = minutes % 60
        if hours < 24:
            return f"{hours}h {remaining_m}m"
        days = hours // 24
        remaining_h = hours % 24
        return f"{days}d {remaining_h}h"
    except Exception:
        return ""


def filter_threads_by_fields(
    threads: list,
    fields: str,
) -> list[dict]:
    """Filter threads to only include specified fields.

    This produces a flat list of thread summaries for reduced token usage.

    Args:
        threads: List of CommentThread objects
        fields: Comma-separated field names

    Available fields:
        index - Thread index (0-based)
        file - File path
        line - Line number
        message - Root comment message
        author - Author name
        resolved - Whether thread is resolved
        patch_set - Patchset number
        code_context - Code context around comment
        replies - Reply messages

    Returns:
        List of dicts with only the requested fields per thread
    """
    field_list = [f.strip() for f in fields.split(",")]
    result = []

    for idx, thread in enumerate(threads):
        thread_data = {}
        root = thread.root_comment

        for field in field_list:
            if field == "index":
                thread_data["index"] = idx
            elif field == "file":
                thread_data["file"] = root.file_path
            elif field == "line":
                thread_data["line"] = root.line
            elif field == "message":
                thread_data["message"] = root.message
            elif field == "author":
                thread_data["author"] = root.author.name
            elif field == "resolved":
                thread_data["resolved"] = thread.is_resolved
            elif field == "patch_set":
                thread_data["patch_set"] = root.patch_set
            elif field == "code_context":
                if root.code_context:
                    thread_data["code_context"] = root.code_context.to_dict()
                else:
                    thread_data["code_context"] = None
            elif field == "replies":
                thread_data["replies"] = [
                    {"author": r.author.name, "message": r.message}
                    for r in thread.replies
                ]

        result.append(thread_data)

    return result


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout."""
    print(format_json(envelope, pretty=pretty))


def output_success(
    data: Any,
    command: str,
    pretty: bool,
    next_actions: list[str] | None = None,
) -> None:
    """Output success envelope to stdout."""
    envelope = success_response(data, command, next_actions=next_actions)
    output_result(envelope, pretty)


def output_error(code: str, message: str, command: str, pretty: bool) -> int:
    """Output error envelope to stdout and return exit code."""
    envelope = error_response_from_dict(code, message, command)
    output_result(envelope, pretty)
    return ExitCode.GENERAL_ERROR


def generate_review_prompt(url: str) -> str:
    """Generate a prompt for AI-assisted patch series review.

    Args:
        url: URL to any patch in the series

    Returns:
        Formatted prompt string
    """
    return f"""Address comments on this patch series.

Start: gerrit review-series {url}
  (shows series, checks out first patch with comments)

For each patch:
  1. Review comments shown, make fixes
  2. Stage replies:  gerrit stage --done <index>
                     gerrit stage <index> "message"
  3. Commit:         git add <files> && git commit --amend --no-edit
  4. Next patch:     gerrit finish-patch
     (rebases descendants, advances to next patch with comments)

For substantive issues, ask me before making changes.

When done: gerrit end-session
To abort: gerrit abort-session (discards all changes)"""


def cmd_extract(args):
    """Extract comments from a Gerrit change."""
    command = "extract"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)
    fields = getattr(args, 'fields', None)

    try:
        include_system = getattr(args, 'include_system', False)
        result = extract_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
            include_system=include_system,
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
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        # Extract to get the threads
        result = extract_comments(
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
        replier = CommentReplier()
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
    command = "done"
    pretty = getattr(args, 'pretty', False)

    try:
        # Parse URL to get change number
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get the threads
        result = extract_comments(
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
        replier = CommentReplier()
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
    command = "ack"
    pretty = getattr(args, 'pretty', False)

    try:
        # Parse URL to get change number
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get the threads
        result = extract_comments(
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
        replier = CommentReplier()
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
    command = "batch-reply"
    pretty = getattr(args, 'pretty', False)

    try:
        # Load replies from JSON file
        with open(args.file) as f:
            replies_data = json_module.load(f)

        # Parse URL
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)

        # Extract to get threads
        result = extract_comments(
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
        replier = CommentReplier()
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


def cmd_review(args):
    """Get code changes for review, optionally post review comments."""
    import json as json_module
    command = "review"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)

    try:
        reviewer = CodeReviewer()

        # Determine context lines for diff
        if getattr(args, 'full_context', False):
            context_lines = None  # Full file context (Gerrit default, verbose)
        elif getattr(args, 'changes_only', False):
            context_lines = 0    # Changed lines only, no context
        else:
            context_lines = getattr(args, 'unified', 3)

        # Get review data
        review_data = reviewer.get_review_data(
            url=args.url,
            include_file_content=args.full_content,
            context_lines=context_lines,
        )

        # If posting comments from file
        if args.post_comments:
            with open(args.post_comments) as f:
                review_spec = json_module.load(f)

            # Handle dry-run mode
            dry_run = getattr(args, 'dry_run', False)
            if dry_run:
                data = {
                    "dry_run": True,
                    "change_number": review_data.change_info.change_number,
                    "would_post": {
                        "comments": review_spec.get('comments', []),
                        "message": review_spec.get('message'),
                        "vote": review_spec.get('vote'),
                        "comment_count": len(review_spec.get('comments', [])),
                    },
                }
                output_success(data, command, pretty)
                sys.exit(ExitCode.SUCCESS)

            result = reviewer.post_review(
                change_number=review_data.change_info.change_number,
                comments=review_spec.get('comments', []),
                message=review_spec.get('message'),
                vote=review_spec.get('vote'),
            )

            if result.success:
                data = {
                    "success": True,
                    "comments_posted": result.comments_posted,
                    "vote": result.vote,
                }
                output_success(data, command, pretty)
                sys.exit(ExitCode.SUCCESS)
            else:
                sys.exit(output_error(ErrorCode.API_ERROR, result.error or "Unknown error", command, pretty))

        # Output the review data
        data = review_data.to_dict()
        if summary_lines is not None:
            data = truncate_review_data(data, summary_lines)

        output_success(
            data, command, pretty,
            next_actions=[
                "gc comments <URL> -- see unresolved comments",
                "gc review <URL> --post-comments <file> -- post review comments",
            ],
        )
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_series_comments(args):
    """Get all unresolved comments from all patches in a series."""
    command = "series-comments"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)
    fields = getattr(args, 'fields', None)

    try:
        include_system = getattr(args, 'include_system', False)
        finder = SeriesFinder()
        result = finder.get_series_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
            show_progress=False,  # No progress in JSON mode
            include_system=include_system,
        )

        if fields:
            # Output filtered threads per patch (--fields takes precedence)
            patches_data = []
            for patch in result.patches_with_comments:
                patches_data.append({
                    "change_number": patch.change_number,
                    "subject": patch.subject,
                    "threads": filter_threads_by_fields(patch.threads, fields),
                })
            data = {
                "total_unresolved": result.total_unresolved,
                "patches": patches_data,
            }
        else:
            data = result.to_dict()
            if summary_lines is not None:
                data = truncate_series_comments(data, summary_lines)

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error getting series comments: {e}", command, pretty))


def cmd_series(args):
    """Find all patches in a series and show AI review prompt."""
    command = "series"
    pretty = getattr(args, 'pretty', False)

    try:
        # Check git state FIRST before any slow operations (fail fast)
        no_checkout = getattr(args, 'no_checkout', False)
        if not no_checkout and not (args.urls_only or args.numbers_only):
            manager = RebaseManager()
            ready, msg = manager.check_git_repo()
            if not ready:
                sys.exit(output_error(ErrorCode.GIT_ERROR, msg, command, pretty))

        finder = SeriesFinder()
        series = finder.find_series(
            url=args.url,
            include_abandoned=args.include_abandoned,
        )

        # Special output modes (plain text, not JSON)
        if args.urls_only:
            for patch in series.patches:
                print(patch.url)
            sys.exit(ExitCode.SUCCESS)
        elif args.numbers_only:
            for patch in series.patches:
                print(patch.change_number)
            sys.exit(ExitCode.SUCCESS)

        # Fetch comment counts for each patch
        patch_comments = {}
        for patch in series.patches:
            try:
                result = extract_comments(
                    url=patch.url,
                    include_resolved=False,
                    include_code_context=False,
                )
                patch_comments[patch.change_number] = len(result.threads)
            except Exception:
                patch_comments[patch.change_number] = -1  # Error fetching

        # Build patches with comment counts
        patches_with_comments = [cn for cn, count in patch_comments.items() if count > 0]
        first_with_comments = patches_with_comments[0] if patches_with_comments else None

        # Checkout (unless --no-checkout)
        checkout_result = None
        if not no_checkout:
            target_change = first_with_comments or series.patches[0].change_number
            success, message = work_on_patch(args.url, target_change)
            checkout_result = {
                "success": success,
                "change_number": target_change,
                "message": message,
            }

        # Build response data
        data = {
            "series": series.to_dict(),
            "comment_counts": patch_comments,
            "patches_with_comments": patches_with_comments,
            "checkout": checkout_result,
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error finding series: {e}", command, pretty))


def cmd_interactive(args):
    """Run interactive mode for reviewing series comments."""
    try:
        if args.vim:
            run_interactive_vim(args.url)
        else:
            run_interactive(args.url)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error in interactive mode: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_series_status(args):
    """Show status dashboard for a patch series."""
    command = "series-status"
    pretty = getattr(args, 'pretty', False)

    try:
        result = show_series_status(args.url, output_json=True)
        # Result is already JSON string, parse and re-output with envelope
        import json as json_module
        data = json_module.loads(result)
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_work_on_patch(args):
    """Start working on a specific patch in a series."""
    import os as _os
    try:
        target = args.target

        # Resolve target to (change_number, url)
        if target.lstrip("-").isdigit():
            # Plain change number - derive URL
            change_number = int(target)
            gerrit_base = _os.environ.get("GERRIT_URL", "").rstrip("/")
            if gerrit_base:
                url = f"{gerrit_base}/{change_number}"
            else:
                print("Error: Set GERRIT_URL environment variable to use change numbers directly.", file=sys.stderr)
                sys.exit(1)
        else:
            # Full or short URL - parse it
            try:
                _, change_number = GerritCommentsClient.parse_gerrit_url(target)
                url = target
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        success, message = work_on_patch(url, change_number)
        print(message)
        if not success:
            sys.exit(1)
        # Remember URL so `gc reply` works immediately without re-running `gc comments`
        LastURLManager().save(url, change_number)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_next_patch(args):
    """Move to the next patch in the series."""
    try:
        success, message = next_patch(with_comments=args.with_comments)
        print(message)
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_finish_patch(args):
    """Finish working on the current patch."""
    try:
        auto_next = not getattr(args, 'stay', False)
        success, message = finish_patch(auto_next=auto_next)
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_abort(args):
    """End the current session (abort or keep changes)."""
    try:
        if getattr(args, 'keep_changes', False):
            success, message = end_session()
        else:
            success, message = abort_patch()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    """Show current rebase session status."""
    try:
        has_session, message = rebase_status()
        print(message)
        if not has_session:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_continue_reintegration(args):
    """Continue reintegration after conflict resolution."""
    try:
        from .rebase import RebaseManager
        manager = RebaseManager()
        success, message = manager.continue_reintegration()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_skip_reintegration(args):
    """Skip the current change during reintegration."""
    try:
        from .rebase import RebaseManager
        manager = RebaseManager()
        success, message = manager.skip_reintegration()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_stage(args):
    """Stage a comment reply without posting."""
    try:
        # Get URL from args or session
        url = args.url
        if url is None:
            # Try to get from active session
            session_info = get_session_info()
            if session_info:
                # Construct URL for current patch
                target_change = session_info['target_change']
                base = session_info['series_url'].rsplit('/', 1)[0]
                url = f"{base}/{target_change}"
                print(f"Using current patch: {target_change}")
            else:
                print("Error: No URL provided and no active session.", file=sys.stderr)
                print("Start a session with: gerrit work-on-patch <change_or_url>", file=sys.stderr)
                sys.exit(1)

        # Parse URL to get change number
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(url)

        # Extract to get the threads
        result = extract_comments(
            url=url,
            include_resolved=False,
            include_code_context=False,
        )

        if args.thread_index >= len(result.threads):
            print(f"Error: Thread index {args.thread_index} out of range. Only {len(result.threads)} threads.", file=sys.stderr)
            sys.exit(1)

        thread = result.threads[args.thread_index]

        # Determine message and resolved status
        if args.done:
            message = args.message or "Done"
            resolve = True
        elif args.ack:
            message = args.message or "Acknowledged"
            resolve = True
        else:
            message = args.message
            resolve = args.resolve

        if not message:
            print("Error: Message is required (or use --done/--ack)", file=sys.stderr)
            sys.exit(1)

        # Get last comment in thread
        last_comment = thread.replies[-1] if thread.replies else thread.root_comment

        # Get current patchset from change detail
        client = GerritCommentsClient()
        change = client.get_change_detail(change_number)
        current_revision = change.get("current_revision", "")
        current_patchset = change.get("revisions", {}).get(current_revision, {}).get("_number", 0)

        # Stage the operation
        staging_mgr = StagingManager()
        staging_mgr.stage_operation(
            change_number=change_number,
            thread_index=args.thread_index,
            file_path=last_comment.file_path,
            line=last_comment.line,
            message=message,
            resolve=resolve,
            comment_id=last_comment.id,
            patchset=current_patchset,
            change_url=result.change_info.url,
        )

        action = "resolve" if resolve else "comment on"
        loc = f"{last_comment.file_path}:{last_comment.line or 'patchset'}"
        print(f"✓ Staged operation to {action} {loc}")
        print(f"  Message: \"{message[:50]}{'...' if len(message) > 50 else ''}\"")
        print(f"\nUse 'gerrit push {change_number}' to post all staged operations")

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error staging operation: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_push(args):
    """Push all staged operations for a change."""
    try:
        replier = CommentReplier()
        success, message, count = replier.push_staged(
            change_number=args.change_number,
            dry_run=args.dry_run,
        )

        print(message)

        if not success:
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_staged_list(args):
    """List all patches with staged operations."""
    command = "staged.list"
    pretty = getattr(args, 'pretty', False)

    try:
        staging_mgr = StagingManager()
        staged_patches = staging_mgr.list_all_staged()

        data = {
            "staged_patches": [
                {
                    "change_number": sp.change_number,
                    "patchset": sp.patchset,
                    "operation_count": len(sp.operations),
                }
                for sp in staged_patches
            ],
            "total_patches": len(staged_patches),
        }

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_staged_show(args):
    """Show staged operations for a specific patch."""
    command = "staged.show"
    pretty = getattr(args, 'pretty', False)

    try:
        staging_mgr = StagingManager()
        staged = staging_mgr.load_staged(args.change_number)

        if staged is None or not staged.operations:
            data = {
                "change_number": args.change_number,
                "staged": None,
            }
            output_success(data, command, pretty)
            sys.exit(ExitCode.SUCCESS)

        output_success(staged.to_dict(), command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_staged_remove(args):
    """Remove a specific staged operation."""
    try:
        staging_mgr = StagingManager()
        success = staging_mgr.remove_operation(args.change_number, args.operation_index)

        if success:
            print(f"✓ Removed operation {args.operation_index} from change {args.change_number}")
        else:
            print("✗ Failed to remove operation (check change number and index)", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_staged_clear(args):
    """Clear staged operations for a patch (or all if no change specified)."""
    try:
        staging_mgr = StagingManager()
        change_number = getattr(args, 'change_number', None)
        if change_number:
            staging_mgr.clear_staged(change_number)
            print(f"✓ Cleared all staged operations for change {change_number}")
        else:
            count = staging_mgr.clear_all_staged()
            print(f"✓ Cleared staged operations for {count} change(s)")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_staged_refresh(args):
    """Refresh patchset number for staged operations."""
    try:
        staging_mgr = StagingManager()
        client = GerritCommentsClient()

        # Get current patchset
        change = client.get_change_detail(args.change_number)
        current_revision = change.get("current_revision", "")
        current_patchset = change.get("revisions", {}).get(current_revision, {}).get("_number", 0)

        if current_patchset == 0:
            print(f"Error: Could not determine current patchset for change {args.change_number}", file=sys.stderr)
            sys.exit(1)

        # Update staged patchset
        success = staging_mgr.update_patchset(args.change_number, current_patchset)

        if success:
            print(f"✓ Updated staged operations for change {args.change_number} to patchset {current_patchset}")
        else:
            print(f"✗ No staged operations found for change {args.change_number}", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_reviewers(args):
    """List reviewers on a change."""
    command = "reviewers"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()
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
    command = "add-reviewer"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

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
    command = "remove-reviewer"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

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


def cmd_abandon(args):
    """Abandon a Gerrit change."""
    command = "abandon"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

        message = getattr(args, 'message', '') or ''

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
    command = "maloo"
    pretty = getattr(args, 'pretty', False)

    try:
        urls = args.url  # Now a list (nargs='+')
        client = GerritCommentsClient()
        patchset = getattr(args, 'patchset', None)

        if len(urls) == 1:
            # Single URL mode - same output format as before
            base_url, change_number = GerritCommentsClient.parse_gerrit_url(urls[0])
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
                    _, change_number = GerritCommentsClient.parse_gerrit_url(url)
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
    command = "info"
    pretty = getattr(args, 'pretty', False)
    show_bots = getattr(args, 'show_bots', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

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

        client = GerritCommentsClient()
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
                _, change_number = GerritCommentsClient.parse_gerrit_url(
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


def cmd_message(args):
    """Post a top-level message on a Gerrit change."""
    command = "message"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

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


def cmd_checkout(args):
    """Fetch and checkout a Gerrit change."""
    import subprocess

    command = "checkout"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = GerritCommentsClient.parse_gerrit_url(args.url)
        client = GerritCommentsClient()

        # Get change detail to find current patchset
        change = client.get_change_detail(change_number)
        current_revision = change.get("current_revision", "")
        revisions = change.get("revisions", {})
        subject = change.get("subject", "")

        # Use specified patchset or current
        patchset = getattr(args, 'patchset', None)
        if patchset:
            rev_info = None
            for rev_id, rev_data in revisions.items():
                if rev_data.get("_number") == patchset:
                    rev_info = rev_data
                    break
            if not rev_info:
                sys.exit(output_error(
                    ErrorCode.NOT_FOUND,
                    f"Patchset {patchset} not found for change {change_number}",
                    command, pretty
                ))
        else:
            patchset = revisions.get(current_revision, {}).get("_number")
            if not patchset:
                sys.exit(output_error(
                    ErrorCode.API_ERROR,
                    f"Could not determine current patchset for change {change_number}",
                    command, pretty
                ))

        # Build Gerrit ref: refs/changes/XX/NNNNN/PS
        suffix = str(change_number)[-2:].zfill(2)
        ref = f"refs/changes/{suffix}/{change_number}/{patchset}"

        # Build fetch URL - need SSH or HTTPS for Gerrit refs
        # (git:// protocol often doesn't expose refs/changes/)
        from urllib.parse import urlparse
        gerrit_host = urlparse(base_url).hostname
        ssh_port = "29418"

        # Try to find an SSH remote URL
        fetch_url = None
        try:
            result = subprocess.run(
                ["git", "remote", "-v"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5,
            )
            if result.returncode == 0:
                import re as _re
                for line in result.stdout.decode().splitlines():
                    if gerrit_host in line and "ssh://" in line:
                        match = _re.search(r'(ssh://\S+)', line)
                        if match:
                            fetch_url = match.group(1)
                            break
        except Exception:
            pass

        if not fetch_url:
            # Discover SSH user same way as abandon command
            ssh_user = client._discover_ssh_user(gerrit_host)
            if ssh_user:
                fetch_url = (
                    f"ssh://{ssh_user}@{gerrit_host}:{ssh_port}"
                    f"/fs/lustre-release"
                )
            else:
                fetch_url = "origin"

        # Fetch the ref
        print(f"Fetching change {change_number} patchset {patchset} ({subject})...")
        fetch_result = subprocess.run(
            ["git", "fetch", fetch_url, ref],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120,
        )
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr.decode().strip()
            sys.exit(output_error(
                ErrorCode.API_ERROR,
                f"git fetch failed: {stderr}",
                command, pretty
            ))

        # Get FETCH_HEAD
        fh_result = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        commit = fh_result.stdout.decode().strip()

        # Checkout
        detach = not getattr(args, 'branch', None)
        if detach:
            co_result = subprocess.run(
                ["git", "checkout", commit],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        else:
            branch_name = args.branch
            co_result = subprocess.run(
                ["git", "checkout", "-b", branch_name, commit],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        if co_result.returncode != 0:
            stderr = co_result.stderr.decode().strip()
            sys.exit(output_error(
                ErrorCode.API_ERROR,
                f"git checkout failed: {stderr}",
                command, pretty
            ))

        data = {
            "change_number": change_number,
            "patchset": patchset,
            "subject": subject,
            "commit": commit[:12],
            "ref": ref,
        }
        if not detach:
            data["branch"] = args.branch

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))


def cmd_find_user(args):
    """Search for users by name."""
    command = "find-user"
    pretty = getattr(args, 'pretty', False)

    try:
        client = GerritCommentsClient()

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


# Command explanations with detailed usage and examples
COMMAND_EXPLANATIONS = {
    "comments": {
        "summary": "Get unresolved comments from a Gerrit change",
        "description": """
The 'comments' command extracts comment threads from a Gerrit change URL.
By default, it only shows unresolved comments. Use --all to include resolved ones.

Each comment thread is assigned an index (0, 1, 2, ...) that you can use with
other commands like 'reply' or 'stage'.
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
        ],
        "related": ["reply", "stage", "series-comments"],
    },
    "reply": {
        "summary": "Reply to a comment thread",
        "description": """
The 'reply' command posts a reply to a specific comment thread. You identify
the thread by its index from the 'comments' output.

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
2. Auto-advance to the next patch with comments (unless --stay)

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
Can also post review comments from a JSON file.
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
        ],
        "related": ["comments", "review-series"],
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
        "summary": "List reviewers on a change",
        "description": """
Show all reviewers and their votes on a Gerrit change.
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
    "find-user": {
        "summary": "Search for users by name",
        "description": """
Search for Gerrit users by name, email, or username. Useful for finding
the exact username before adding as a reviewer.
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
Use --vim for a vim-based interface with tmux.
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
            {
                "command": "gc interactive --vim URL",
                "description": "Use vim-based interface",
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
}

# Aliases for command lookup
COMMAND_ALIASES = {
    "extract": "comments",
    "i": "interactive",
}


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
            ("gc reviewers URL", "List current reviewers"),
            ("gc find-user john", "Search for users by name"),
            ("gc add-reviewer URL username", "Add a reviewer"),
            ("gc add-reviewer --cc URL username", "Add as CC only"),
            ("gc remove-reviewer URL username", "Remove a reviewer"),
        ],
    },
}


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
    from .describe import get_tool_description

    pretty = getattr(args, 'pretty', False)
    command_name = getattr(args, 'command_name', None)
    tool_desc = get_tool_description()

    if command_name:
        normalized = command_name.replace(".", " ")
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


class _JsonErrorParser(argparse.ArgumentParser):
    """ArgumentParser that outputs errors as JSON instead of stderr.

    Used as parser_class for subparsers so that argument errors from
    any subcommand produce structured JSON output.
    """

    def error(self, message: str) -> None:
        envelope = error_response_from_dict(
            ErrorCode.INVALID_INPUT,
            message,
            "cli",
        )
        print(format_json(envelope))
        sys.exit(ExitCode.INVALID_INPUT)


def main():
    """Main entry point."""
    from .parsers import setup_parsers

    parser = argparse.ArgumentParser(
        description="Extract and reply to Gerrit review comments. "
                    "Run 'gc describe' for machine-readable API documentation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Use _JsonErrorParser for subparsers so argument errors from
    # subcommands also produce JSON output. The top-level parser is
    # left as standard ArgumentParser so tests that mock it still work.
    subparsers = parser.add_subparsers(
        dest="command", help="Command to run",
        parser_class=_JsonErrorParser,
    )

    # Map command names to handler functions
    handlers = {
        'comments': cmd_extract,
        'reply': cmd_reply,
        'batch': cmd_batch_reply,
        'review': cmd_review,
        'series_comments': cmd_series_comments,
        'series': cmd_series,
        'series_status': cmd_series_status,
        'interactive': cmd_interactive,
        'work_on_patch': cmd_work_on_patch,
        'next_patch': cmd_next_patch,
        'finish_patch': cmd_finish_patch,
        'abort': cmd_abort,
        'status': cmd_status,
        'stage': cmd_stage,
        'push': cmd_push,
        'staged_list': cmd_staged_list,
        'staged_show': cmd_staged_show,
        'staged_remove': cmd_staged_remove,
        'staged_clear': cmd_staged_clear,
        'staged_refresh': cmd_staged_refresh,
        'continue_reintegration': cmd_continue_reintegration,
        'skip_reintegration': cmd_skip_reintegration,
        'reviewers': cmd_reviewers,
        'add_reviewer': cmd_add_reviewer,
        'remove_reviewer': cmd_remove_reviewer,
        'find_user': cmd_find_user,
        'abandon': cmd_abandon,
        'checkout': cmd_checkout,
        'maloo': cmd_maloo,
        'info': cmd_info,
        'watch': cmd_watch,
        'message': cmd_message,
        'explain': cmd_explain,
        'examples': cmd_examples,
        'done': cmd_done,
        'ack': cmd_ack,
        'describe': cmd_describe,
    }

    setup_parsers(subparsers, handlers)

    args = parser.parse_args()

    if not args.command:
        # If there's an active session, show status by default
        from .rebase import RebaseManager
        manager = RebaseManager()
        if manager.has_active_session():
            cmd_status(args)
        else:
            parser.print_help()
            sys.exit(1)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
