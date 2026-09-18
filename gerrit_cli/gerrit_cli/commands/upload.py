"""Upload command: push HEAD, or a series ending at HEAD, to Gerrit."""

import sys

from ..envelope import error_response_from_dict
from ..errors import ExitCode
from ..upload import UploadError, upload
from ._helpers import _cli, output_result, output_success


def cmd_upload(args):
    """Push HEAD to refs/for/<branch> as the selected credential set."""
    cli = _cli()
    command = "upload"
    pretty = getattr(args, "pretty", False)

    try:
        data = upload(
            cli.GerritCommentsClient(),
            repo=args.repo,
            change=args.change,
            branch=args.branch,
            project=args.project,
            topic=args.topic,
            dry_run=args.dry_run,
            amend=not args.no_amend,
            series=args.series,
        )
    except UploadError as e:
        output_result(
            error_response_from_dict(
                e.code, e.message, command, details=e.details
            ),
            pretty,
        )
        sys.exit(e.exit_code)

    output_success(data, command, pretty)
    sys.exit(ExitCode.SUCCESS)
