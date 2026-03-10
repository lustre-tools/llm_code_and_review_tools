"""Tests for the parsers module."""

import argparse
from unittest.mock import MagicMock


class TestParserHandlerIntegration:
    """Test that parsers define all attributes expected by command handlers."""

    def test_review_series_parser_has_required_attributes(self):
        """Test review-series parser defines all attributes used by cmd_series.

        This test catches the bug where add_review_series_parser was missing
        urls_only, numbers_only, include_abandoned, no_prompt, checkout args.
        """
        from gerrit_cli.parsers import add_review_series_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_review_series_parser(subparsers)

        # Parse with minimal args
        args = parser.parse_args(['review-series', 'https://example.com/12345'])

        # These are all accessed by cmd_series - verify they exist
        assert hasattr(args, 'url')
        assert hasattr(args, 'pretty')
        assert hasattr(args, 'urls_only')
        assert hasattr(args, 'numbers_only')
        assert hasattr(args, 'include_abandoned')
        assert hasattr(args, 'no_prompt')
        assert hasattr(args, 'checkout')

        # Verify default values
        assert args.pretty is False
        assert args.urls_only is False
        assert args.numbers_only is False
        assert args.include_abandoned is False
        assert args.no_prompt is False
        assert args.checkout is False

    def test_review_series_parser_options(self):
        """Test review-series parser accepts all options."""
        from gerrit_cli.parsers import add_review_series_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_review_series_parser(subparsers)

        # Test --urls-only
        args = parser.parse_args(['review-series', 'https://example.com/12345', '--urls-only'])
        assert args.urls_only is True

        # Test --numbers-only
        args = parser.parse_args(['review-series', 'https://example.com/12345', '-n'])
        assert args.numbers_only is True

        # Test --include-abandoned
        args = parser.parse_args(['review-series', 'https://example.com/12345', '-a'])
        assert args.include_abandoned is True

        # Test --no-prompt
        args = parser.parse_args(['review-series', 'https://example.com/12345', '--no-prompt'])
        assert args.no_prompt is True

        # Test --checkout
        args = parser.parse_args(['review-series', 'https://example.com/12345', '-c'])
        assert args.checkout is True

    def test_setup_parsers_creates_all_commands(self):
        """Test that setup_parsers creates all expected commands."""
        from gerrit_cli.parsers import setup_parsers

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest='command')

        # Create mock handlers for all commands
        handlers = {
            'comments': MagicMock(),
            'reply': MagicMock(),
            'batch': MagicMock(),
            'review': MagicMock(),
            'series_comments': MagicMock(),
            'series': MagicMock(),
            'series_status': MagicMock(),
            'interactive': MagicMock(),
            'work_on_patch': MagicMock(),
            'next_patch': MagicMock(),
            'finish_patch': MagicMock(),
            'abort': MagicMock(),
            'status': MagicMock(),
            'stage': MagicMock(),
            'push': MagicMock(),
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
            'continue_reintegration': MagicMock(),
            'skip_reintegration': MagicMock(),
            'reviewers': MagicMock(),
            'add_reviewer': MagicMock(),
            'remove_reviewer': MagicMock(),
            'find_user': MagicMock(),
            'abandon': MagicMock(),
            'checkout': MagicMock(),
            'maloo': MagicMock(),
            'info': MagicMock(),
            'series_info': MagicMock(),
            'watch': MagicMock(),
            'set_topic': MagicMock(),
            'restore': MagicMock(),
            'rebase': MagicMock(),
            'vote': MagicMock(),
            'diff': MagicMock(),
            'message': MagicMock(),
            'search': MagicMock(),
            'explain': MagicMock(),
            'examples': MagicMock(),
            'done': MagicMock(),
            'ack': MagicMock(),
            'describe': MagicMock(),
        }

        setup_parsers(subparsers, handlers)

        # Verify key commands can be parsed
        commands_to_test = [
            ('review-series', ['review-series', 'https://example.com/12345']),
            ('work-on-patch', ['work-on-patch', '12345']),
            ('finish-patch', ['finish-patch']),
            ('stage', ['stage', '0', 'Done']),
            ('abort', ['abort']),
            ('status', ['status']),
        ]

        for cmd_name, argv in commands_to_test:
            args = parser.parse_args(argv)
            assert args.command == cmd_name, f"Command {cmd_name} not parsed correctly"
            assert hasattr(args, 'func'), f"Command {cmd_name} has no func attribute"


class TestParserCreation:
    """Test that all parser creation functions work."""

    def test_add_extract_parser(self):
        """Test extract parser is created correctly."""
        from gerrit_cli.parsers import add_extract_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_extract_parser(subparsers)

        # Parse valid args
        args = parser.parse_args(['extract', 'https://example.com/12345'])
        assert args.url == 'https://example.com/12345'
        assert args.all is False
        assert args.pretty is False

    def test_add_extract_parser_with_options(self):
        """Test extract parser accepts all options."""
        from gerrit_cli.parsers import add_extract_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_extract_parser(subparsers)

        args = parser.parse_args([
            'extract', 'https://example.com/12345',
            '--all', '--pretty', '--no-context', '--context-lines', '5'
        ])
        assert args.all is True
        assert args.pretty is True
        assert args.no_context is True
        assert args.context_lines == 5

    def test_add_reply_parser(self):
        """Test reply parser is created correctly."""
        from gerrit_cli.parsers import add_reply_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_reply_parser(subparsers)

        args = parser.parse_args([
            'reply', '--url', 'https://example.com/12345', '0', 'Done'
        ])
        assert args.url == 'https://example.com/12345'
        assert args.thread_index == 0
        assert args.message == 'Done'

    def test_add_reply_parser_done_flag(self):
        """Test reply parser with --done flag."""
        from gerrit_cli.parsers import add_reply_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_reply_parser(subparsers)

        args = parser.parse_args([
            'reply', '0', '--done'
        ])
        assert args.done is True
        assert args.ack is False

    def test_add_stage_reply_parser(self):
        """Test stage parser is created correctly."""
        from gerrit_cli.parsers import add_stage_reply_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_stage_reply_parser(subparsers)

        args = parser.parse_args(['stage', '0', 'Fixed'])
        assert args.thread_index == 0
        assert args.message == 'Fixed'

    def test_add_stage_reply_parser_done_flag(self):
        """Test stage parser with --done flag."""
        from gerrit_cli.parsers import add_stage_reply_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_stage_reply_parser(subparsers)

        args = parser.parse_args(['stage', '0', '--done'])
        assert args.done is True
        assert args.thread_index == 0

    def test_add_work_on_patch_parser(self):
        """Test work-on-patch parser is created correctly."""
        from gerrit_cli.parsers import add_work_on_patch_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_work_on_patch_parser(subparsers)

        args = parser.parse_args(['work-on-patch', '12345'])
        assert args.target == '12345'

    def test_add_work_on_patch_parser_with_url(self):
        """Test work-on-patch parser with URL."""
        from gerrit_cli.parsers import add_work_on_patch_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_work_on_patch_parser(subparsers)

        args = parser.parse_args([
            'work-on-patch', 'https://example.com/12345'
        ])
        assert args.target == 'https://example.com/12345'

    def test_add_finish_patch_parser(self):
        """Test finish-patch parser is created correctly."""
        from gerrit_cli.parsers import add_finish_patch_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_finish_patch_parser(subparsers)

        args = parser.parse_args(['finish-patch'])
        assert args.stay is False

    def test_add_finish_patch_parser_stay(self):
        """Test finish-patch parser with --stay."""
        from gerrit_cli.parsers import add_finish_patch_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_finish_patch_parser(subparsers)

        args = parser.parse_args(['finish-patch', '--stay'])
        assert args.stay is True

    def test_add_push_parser(self):
        """Test push parser is created correctly."""
        from gerrit_cli.parsers import add_push_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_push_parser(subparsers)

        args = parser.parse_args(['push', '12345'])
        assert args.change_number == 12345
        assert args.dry_run is False

    def test_add_push_parser_dry_run(self):
        """Test push parser with --dry-run."""
        from gerrit_cli.parsers import add_push_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_push_parser(subparsers)

        args = parser.parse_args(['push', '12345', '--dry-run'])
        assert args.dry_run is True


class TestConsolidatedParsers:
    """Tests for the consolidated abort and staged parsers."""

    def test_abort_parser_default(self):
        """Test abort parser default (no --keep-changes)."""
        from gerrit_cli.parsers import add_abort_session_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_abort_session_parser(subparsers)

        args = parser.parse_args(['abort'])
        assert args.keep_changes is False

    def test_abort_parser_keep_changes(self):
        """Test abort parser with --keep-changes."""
        from gerrit_cli.parsers import add_abort_session_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_abort_session_parser(subparsers)

        args = parser.parse_args(['abort', '--keep-changes'])
        assert args.keep_changes is True

    def test_abort_parser_keep_changes_short(self):
        """Test abort parser with -k short flag."""
        from gerrit_cli.parsers import add_abort_session_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_abort_session_parser(subparsers)

        args = parser.parse_args(['abort', '-k'])
        assert args.keep_changes is True

    def test_staged_parser_default_list(self):
        """Test staged command defaults to list subcommand."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged'])
        assert args.staged_command == 'list'

    def test_staged_parser_list(self):
        """Test staged list subcommand."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'list'])
        assert args.staged_command == 'list'
        assert hasattr(args, 'pretty')

    def test_staged_parser_show(self):
        """Test staged show subcommand."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'show', '12345'])
        assert args.staged_command == 'show'
        assert args.change_number == 12345

    def test_staged_parser_remove(self):
        """Test staged remove subcommand."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'remove', '12345', '3'])
        assert args.staged_command == 'remove'
        assert args.change_number == 12345
        assert args.operation_index == 3

    def test_staged_parser_clear_one(self):
        """Test staged clear subcommand with change number."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'clear', '12345'])
        assert args.staged_command == 'clear'
        assert args.change_number == 12345

    def test_staged_parser_clear_all(self):
        """Test staged clear subcommand without change number."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'clear'])
        assert args.staged_command == 'clear'
        assert args.change_number is None

    def test_staged_parser_refresh(self):
        """Test staged refresh subcommand."""
        from gerrit_cli.parsers import add_staged_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        handlers = {
            'staged_list': MagicMock(),
            'staged_show': MagicMock(),
            'staged_remove': MagicMock(),
            'staged_clear': MagicMock(),
            'staged_refresh': MagicMock(),
        }
        add_staged_parser(subparsers, handlers)

        args = parser.parse_args(['staged', 'refresh', '12345'])
        assert args.staged_command == 'refresh'
        assert args.change_number == 12345

