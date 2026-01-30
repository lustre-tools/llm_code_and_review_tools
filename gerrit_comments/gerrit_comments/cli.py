#!/usr/bin/env python3
"""Command-line interface for Gerrit comments tools.

This CLI provides commands for:
1. comments - Get unresolved comments from a Gerrit change
2. reply - Reply to comments or mark them as done
3. review - Get diff/changes for code review, optionally post review comments

All commands output JSON by default. Use --pretty for human-readable output.

Examples:
    # Get unresolved comments (JSON output)
    gc comments https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Get comments with human-readable output
    gc comments --pretty https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Reply to a comment (by thread index from comments output)
    gc reply https://review.whamcloud.com/c/fs/lustre-release/+/62796 0 "Done"

    # Mark a comment as done
    gc reply --done https://review.whamcloud.com/c/fs/lustre-release/+/62796 0

    # Get changes for code review
    gc review https://review.whamcloud.com/c/fs/lustre-release/+/62796

    # Get changes with pretty output
    gc review --pretty https://review.whamcloud.com/c/fs/lustre-release/+/62796

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
from .series_status import show_series_status
from .staging import StagingManager
from .summary import truncate_extracted_comments, truncate_review_data, truncate_series_comments


def output_result(envelope: dict[str, Any], pretty: bool) -> None:
    """Output result to stdout."""
    print(format_json(envelope, pretty=pretty))


def output_success(data: Any, command: str, pretty: bool) -> None:
    """Output success envelope to stdout."""
    envelope = success_response(data, command)
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

Start: gerrit-comments review-series {url}
  (shows series, checks out first patch with comments)

For each patch:
  1. Review comments shown, make fixes
  2. Stage replies:  gerrit-comments stage --done <index>
                     gerrit-comments stage <index> "message"
  3. Commit:         git add <files> && git commit --amend --no-edit
  4. Next patch:     gerrit-comments finish-patch
     (rebases descendants, advances to next patch with comments)

For substantive issues, ask me before making changes.

When done: gerrit-comments end-session
To abort: gerrit-comments abort-session (discards all changes)"""


def cmd_extract(args):
    """Extract comments from a Gerrit change."""
    command = "extract"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)

    try:
        result = extract_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
        )

        data = result.to_dict()
        if summary_lines is not None:
            data = truncate_extracted_comments(data, summary_lines)

        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)

    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, f"Error extracting comments: {e}", command, pretty))


def cmd_reply(args):
    """Reply to a comment."""
    command = "reply"
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
            })

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

        # Get review data
        review_data = reviewer.get_review_data(
            url=args.url,
            include_file_content=args.full_content,
        )

        # If posting comments from file
        if args.post_comments:
            with open(args.post_comments) as f:
                review_spec = json_module.load(f)

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

        output_success(data, command, pretty)
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

    try:
        finder = SeriesFinder()
        result = finder.get_series_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
            show_progress=False,  # No progress in JSON mode
        )

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
    try:
        # If URL not provided, try to get it from active session
        url = args.url
        if url is None:
            url = get_session_url()
            if url is None:
                print("Error: No URL provided and no active session.", file=sys.stderr)
                print("Start a session with: gerrit-comments work-on-patch <change> <url>", file=sys.stderr)
                sys.exit(1)
            print(f"Using URL from active session: {url}")

        success, message = work_on_patch(url, args.change_number)
        print(message)
        if not success:
            sys.exit(1)
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
                print("Start a session with: gerrit-comments work-on-patch <change> <url>", file=sys.stderr)
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
        print(f"\nUse 'gerrit-comments push {change_number}' to post all staged operations")

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


def main():
    """Main entry point."""
    from .parsers import setup_parsers

    parser = argparse.ArgumentParser(
        description="Extract and reply to Gerrit review comments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

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
