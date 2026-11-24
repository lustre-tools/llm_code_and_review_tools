"""test-numbers: usable new subtest numbers for lustre/tests scripts."""

import sys

from .. import test_numbers as tn
from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, error_code_for, output_error, output_success


def cmd_test_numbers(args):
    """Which new subtest numbers are free and sit in a gap no open change
    touches; --suggest K picks K of them."""
    cli = _cli()
    command = "test-numbers"
    pretty = getattr(args, "pretty", False)
    if args.family and not tn.TEST_ID_RE.match(args.family):
        sys.exit(output_error(ErrorCode.INVALID_INPUT,
                              f"--family wants a test id like 56x, not {args.family!r}",
                              command, pretty))
    except_branches = None
    if args.except_branches is not None:
        except_branches = [b for b in args.except_branches.split(",") if b]
    try:
        client = cli.GerritCommentsClient()
        result = tn.gather(client, args.files, branch=args.branch,
                           days=args.days, family=args.family,
                           except_branches=except_branches,
                           suggest_k=args.suggest)
    except FileNotFoundError as e:
        sys.exit(output_error(ErrorCode.NOT_FOUND, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
    if args.human:
        print(tn.format_text(result, args.branch, args.days))
    else:
        output_success({"branch": args.branch, "days": args.days,
                        "near_lines": tn.NEAR_LINES, "files": result},
                       command, pretty)
    sys.exit(ExitCode.SUCCESS)
