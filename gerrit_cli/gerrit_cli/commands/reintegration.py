"""Reintegration commands: continue-reintegration, skip-reintegration."""

import sys

from ._helpers import _cli


def cmd_continue_reintegration(args):
    """Continue reintegration after conflict resolution."""
    cli = _cli()
    try:
        manager = cli.RebaseManager()
        success, message = manager.continue_reintegration()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_skip_reintegration(args):
    """Skip the current change during reintegration."""
    cli = _cli()
    try:
        manager = cli.RebaseManager()
        success, message = manager.skip_reintegration()
        print(message)
        if not success:
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
