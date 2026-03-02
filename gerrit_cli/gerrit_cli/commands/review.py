"""Review and series commands: review, series, series-comments, series-status, interactive."""

import sys

from ..errors import ErrorCode, ExitCode
from ..summary import truncate_review_data, truncate_series_comments
from ._helpers import _cli, filter_threads_by_fields, output_error, output_success


def cmd_review(args):
    """Get code changes for review, optionally post review comments."""
    import json as json_module
    cli = _cli()
    command = "review"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)

    try:
        reviewer = cli.CodeReviewer()

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
    cli = _cli()
    command = "series-comments"
    pretty = getattr(args, 'pretty', False)
    summary_lines = getattr(args, 'summary', None)
    fields = getattr(args, 'fields', None)

    try:
        include_system = getattr(args, 'include_system', False)
        include_ci = getattr(args, 'include_ci', False)
        if include_system:
            include_ci = True
        finder = cli.SeriesFinder()
        result = finder.get_series_comments(
            url=args.url,
            include_resolved=args.all,
            include_code_context=not args.no_context,
            context_lines=args.context_lines,
            show_progress=False,  # No progress in JSON mode
            include_system=include_system,
            exclude_ci_bots=not include_ci,
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
    cli = _cli()
    command = "series"
    pretty = getattr(args, 'pretty', False)

    try:
        # Check git state FIRST before any slow operations (fail fast)
        no_checkout = getattr(args, 'no_checkout', False)
        if not no_checkout and not (args.urls_only or args.numbers_only):
            manager = cli.RebaseManager()
            ready, msg = manager.check_git_repo()
            if not ready:
                sys.exit(output_error(ErrorCode.GIT_ERROR, msg, command, pretty))

        finder = cli.SeriesFinder()
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
                result = cli.extract_comments(
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
            success, message = cli.work_on_patch(args.url, target_change)
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
    cli = _cli()
    try:
        if args.vim:
            cli.run_interactive_vim(args.url)
        else:
            cli.run_interactive(args.url)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error in interactive mode: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_series_status(args):
    """Show status dashboard for a patch series."""
    cli = _cli()
    command = "series-status"
    pretty = getattr(args, 'pretty', False)

    try:
        result = cli.show_series_status(args.url, output_json=True)
        # Result is already JSON string, parse and re-output with envelope
        import json as json_module
        data = json_module.loads(result)
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)
    except Exception as e:
        sys.exit(output_error(ErrorCode.API_ERROR, str(e), command, pretty))
