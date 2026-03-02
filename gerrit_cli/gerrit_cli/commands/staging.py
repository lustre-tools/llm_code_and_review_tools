"""Staging commands: stage, push, staged list/show/remove/clear/refresh."""

import sys

from ..errors import ErrorCode, ExitCode
from ..rebase import get_session_info
from ._helpers import _cli, output_error, output_success


def cmd_stage(args):
    """Stage a comment reply without posting."""
    cli = _cli()
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
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(url)

        # Extract to get the threads
        result = cli.extract_comments(
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
        client = cli.GerritCommentsClient()
        change = client.get_change_detail(change_number)
        current_revision = change.get("current_revision", "")
        current_patchset = change.get("revisions", {}).get(current_revision, {}).get("_number", 0)

        # Stage the operation
        staging_mgr = cli.StagingManager()
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
        print(f"\u2713 Staged operation to {action} {loc}")
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
    cli = _cli()
    try:
        replier = cli.CommentReplier()
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
    cli = _cli()
    command = "staged.list"
    pretty = getattr(args, 'pretty', False)

    try:
        staging_mgr = cli.StagingManager()
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
    cli = _cli()
    command = "staged.show"
    pretty = getattr(args, 'pretty', False)

    try:
        staging_mgr = cli.StagingManager()
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
    cli = _cli()
    try:
        staging_mgr = cli.StagingManager()
        success = staging_mgr.remove_operation(args.change_number, args.operation_index)

        if success:
            print(f"\u2713 Removed operation {args.operation_index} from change {args.change_number}")
        else:
            print("\u2717 Failed to remove operation (check change number and index)", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_staged_clear(args):
    """Clear staged operations for a patch (or all if no change specified)."""
    cli = _cli()
    try:
        staging_mgr = cli.StagingManager()
        change_number = getattr(args, 'change_number', None)
        if change_number:
            staging_mgr.clear_staged(change_number)
            print(f"\u2713 Cleared all staged operations for change {change_number}")
        else:
            count = staging_mgr.clear_all_staged()
            print(f"\u2713 Cleared staged operations for {count} change(s)")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_staged_refresh(args):
    """Refresh patchset number for staged operations."""
    cli = _cli()
    try:
        staging_mgr = cli.StagingManager()
        client = cli.GerritCommentsClient()

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
            print(f"\u2713 Updated staged operations for change {args.change_number} to patchset {current_patchset}")
        else:
            print(f"\u2717 No staged operations found for change {args.change_number}", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
