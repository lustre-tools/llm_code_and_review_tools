"""Patch workflow commands: work-on-patch, next-patch, finish-patch, abort, status, checkout."""

import sys

from ..errors import ErrorCode, ExitCode
from ..session import LastURLManager
from ._helpers import _cli, output_error, output_success


def cmd_work_on_patch(args):
    """Start working on a specific patch in a series."""
    import os as _os
    cli = _cli()
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
                _, change_number = cli.GerritCommentsClient.parse_gerrit_url(target)
                url = target
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        success, message = cli.work_on_patch(url, change_number)
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
    cli = _cli()
    try:
        success, message = cli.next_patch(with_comments=args.with_comments)
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
    cli = _cli()
    try:
        auto_next = not getattr(args, 'stay', False)
        success, message = cli.finish_patch(auto_next=auto_next)
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_abort(args):
    """End the current session (abort or keep changes)."""
    cli = _cli()
    try:
        if getattr(args, 'keep_changes', False):
            success, message = cli.end_session()
        else:
            success, message = cli.abort_patch()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    """Show current rebase session status."""
    cli = _cli()
    try:
        has_session, message = cli.rebase_status()
        print(message)
        if not has_session:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_checkout(args):
    """Fetch and checkout a Gerrit change."""
    import subprocess
    cli = _cli()

    command = "checkout"
    pretty = getattr(args, 'pretty', False)

    try:
        base_url, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
        client = cli.GerritCommentsClient()

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
