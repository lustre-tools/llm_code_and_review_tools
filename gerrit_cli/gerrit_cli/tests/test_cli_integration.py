"""CLI integration tests - test the full command line interface wiring.

These tests invoke the CLI as close to real usage as possible, verifying:
1. Parser creates all attributes handlers need
2. CLI entry point routes to correct handlers
3. Error handling works end-to-end

Unlike unit tests that mock at class level, these mock at the HTTP/subprocess
level to test the real code paths.
"""

import argparse
import inspect
from io import StringIO
from unittest.mock import patch

import pytest


class TestParserHandlerContracts:
    """Verify parsers create all attributes that handlers access.

    This catches the class of bugs where a parser is missing arguments
    that the handler expects (like the urls_only bug).
    """

    def get_handler_accessed_attributes(self, handler_func):
        """Extract attribute names accessed via args.X in a handler."""
        import ast
        source = inspect.getsource(handler_func)
        # Dedent the source if needed
        import textwrap
        source = textwrap.dedent(source)

        tree = ast.parse(source)

        accessed = set()
        for node in ast.walk(tree):
            # Look for args.something
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == 'args':
                    accessed.add(node.attr)
        return accessed

    def test_cmd_series_parser_contract(self):
        """Verify review-series parser provides all attributes cmd_series needs."""
        from gerrit_cli.cli import cmd_series
        from gerrit_cli.parsers import add_review_series_parser

        # Get attributes the handler accesses
        accessed = self.get_handler_accessed_attributes(cmd_series)

        # Create parser and parse minimal args
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_review_series_parser(subparsers)
        args = parser.parse_args(['review-series', 'https://example.com/12345'])

        # Verify all accessed attributes exist
        provided = set(vars(args).keys())
        missing = accessed - provided - {'func'}  # func is special

        assert not missing, f"Parser missing attributes used by handler: {missing}"

    def test_cmd_review_parser_contract(self):
        """Verify review parser provides all attributes cmd_review needs."""
        from gerrit_cli.cli import cmd_review
        from gerrit_cli.parsers import add_review_parser

        accessed = self.get_handler_accessed_attributes(cmd_review)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_review_parser(subparsers)
        args = parser.parse_args(['review', 'https://example.com/12345'])

        provided = set(vars(args).keys())
        missing = accessed - provided - {'func'}

        assert not missing, f"Parser missing attributes: {missing}"

    def test_cmd_stage_parser_contract(self):
        """Verify stage parser provides all attributes cmd_stage needs."""
        from gerrit_cli.cli import cmd_stage
        from gerrit_cli.parsers import add_stage_reply_parser

        accessed = self.get_handler_accessed_attributes(cmd_stage)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        add_stage_reply_parser(subparsers)
        args = parser.parse_args(['stage', '0'])

        provided = set(vars(args).keys())
        missing = accessed - provided - {'func'}

        assert not missing, f"Parser missing attributes: {missing}"

    def test_all_commands_have_handlers(self):
        """Verify every command registered has a valid handler."""
        from gerrit_cli import cli
        from gerrit_cli.parsers import setup_parsers

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        # Map command names to handler functions (same as main())
        handlers = {
            'comments': cli.cmd_extract,
            'reply': cli.cmd_reply,
            'batch': cli.cmd_batch_reply,
            'review': cli.cmd_review,
            'series_comments': cli.cmd_series_comments,
            'series': cli.cmd_series,
            'series_status': cli.cmd_series_status,
            'interactive': cli.cmd_interactive,
            'work_on_patch': cli.cmd_work_on_patch,
            'next_patch': cli.cmd_next_patch,
            'finish_patch': cli.cmd_finish_patch,
            'abort': cli.cmd_abort,
            'status': cli.cmd_status,
            'stage': cli.cmd_stage,
            'push': cli.cmd_push,
            'staged_list': cli.cmd_staged_list,
            'staged_show': cli.cmd_staged_show,
            'staged_remove': cli.cmd_staged_remove,
            'staged_clear': cli.cmd_staged_clear,
            'staged_refresh': cli.cmd_staged_refresh,
            'continue_reintegration': cli.cmd_continue_reintegration,
            'skip_reintegration': cli.cmd_skip_reintegration,
            'reviewers': cli.cmd_reviewers,
            'add_reviewer': cli.cmd_add_reviewer,
            'remove_reviewer': cli.cmd_remove_reviewer,
            'find_user': cli.cmd_find_user,
            'abandon': cli.cmd_abandon,
            'checkout': cli.cmd_checkout,
            'maloo': cli.cmd_maloo,
            'info': cli.cmd_info,
            'series_info': cli.cmd_series_info,
            'watch': cli.cmd_watch,
            'set_topic': cli.cmd_set_topic,
            'hashtag': cli.cmd_hashtag,
            'related': cli.cmd_related,
            'restore': cli.cmd_restore,
            'rebase': cli.cmd_rebase,
            'vote': cli.cmd_vote,
            'diff': cli.cmd_diff,
            'message': cli.cmd_message,
            'search': cli.cmd_search,
            'explain': cli.cmd_explain,
            'examples': cli.cmd_examples,
            'done': cli.cmd_done,
            'ack': cli.cmd_ack,
            'describe': cli.cmd_describe,
        }

        setup_parsers(subparsers, handlers)

        # Get all subparsers
        subparsers_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break

        assert subparsers_action is not None, "No subparsers found"

        for cmd_name, _subparser in subparsers_action.choices.items():
            # Parse the command with minimal args
            try:
                # Most commands need at least a URL or number
                if 'series' in cmd_name or 'review' in cmd_name or 'interactive' in cmd_name:
                    test_args = [cmd_name, 'https://example.com/12345']
                elif cmd_name in ('stage',):
                    test_args = [cmd_name, '0']
                elif cmd_name in ('push', 'work-on-patch'):
                    test_args = [cmd_name, '12345']
                elif cmd_name == 'staged':
                    test_args = [cmd_name]  # defaults to list
                else:
                    test_args = [cmd_name]

                args = parser.parse_args(test_args)

                # Verify handler exists and is callable
                assert hasattr(args, 'func'), f"Command {cmd_name} has no handler"
                assert callable(args.func), f"Handler for {cmd_name} is not callable"

            except SystemExit:
                # Some commands require arguments - that's ok
                pass


class TestCLIEntryPoint:
    """Test the main() entry point with real argument parsing."""

    def test_help_output(self):
        """Test that --help works."""
        from gerrit_cli.cli import main

        with patch('sys.argv', ['gerrit-cli', '--help']):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_unknown_command_error(self):
        """Test that unknown commands give helpful error."""
        from gerrit_cli.cli import main

        with patch('sys.argv', ['gerrit-cli', 'not-a-command']):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # argparse exits with 2 for invalid arguments
            assert exc_info.value.code == 2

    def test_status_command_no_session(self):
        """Test status command when no session exists."""
        from gerrit_cli.cli import main
        from gerrit_cli.rebase import RebaseManager

        with patch('sys.argv', ['gerrit-cli', 'status']), \
             patch.object(RebaseManager, 'has_active_session', return_value=False), \
             patch('sys.stdout', new_callable=StringIO) as mock_stdout:
            # cmd_status calls sys.exit(1) when no session
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
            output = mock_stdout.getvalue()
            assert 'No active' in output or 'no active' in output.lower()


class TestGitOperationsReal:
    """Test git operations against real temporary repositories.

    These tests create actual git repos to verify git_utils works correctly.
    """

    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        """Create a real temporary git repository and cd into it."""
        import subprocess

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        # Initialize git repo
        subprocess.run(['git', 'init'], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ['git', 'config', 'user.email', 'test@test.com'],
            cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ['git', 'config', 'user.name', 'Test User'],
            cwd=repo_path, check=True, capture_output=True
        )

        # Create initial commit
        (repo_path / "README.md").write_text("# Test Repo\n")
        subprocess.run(['git', 'add', 'README.md'], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ['git', 'commit', '-m', 'Initial commit'],
            cwd=repo_path, check=True, capture_output=True
        )

        # Change to repo directory (git_utils functions use cwd)
        monkeypatch.chdir(repo_path)

        return repo_path

    def test_check_git_repo(self, git_repo):
        """Test check_git_repo with real repo."""
        from gerrit_cli.git_utils import check_git_repo

        is_repo, msg = check_git_repo()
        assert is_repo is True

    def test_check_not_git_repo(self, tmp_path, monkeypatch):
        """Test check_git_repo with non-repo directory."""
        from gerrit_cli.git_utils import check_git_repo

        non_repo = tmp_path / "not_a_repo"
        non_repo.mkdir()
        monkeypatch.chdir(non_repo)

        is_repo, msg = check_git_repo()
        assert is_repo is False

    def test_get_current_branch(self, git_repo):
        """Test getting current branch."""
        from gerrit_cli.git_utils import get_current_branch

        branch = get_current_branch()
        # Git 2.28+ defaults to 'main', older versions use 'master'
        assert branch in ('main', 'master')

    def test_is_working_tree_clean(self, git_repo):
        """Test clean working tree detection."""
        from gerrit_cli.git_utils import is_working_tree_clean

        # Should be clean initially
        is_clean, msg = is_working_tree_clean()
        assert is_clean is True

        # Modify a file
        (git_repo / "README.md").write_text("# Modified\n")

        # Should be dirty now
        is_clean, msg = is_working_tree_clean()
        assert is_clean is False

    def test_get_current_commit(self, git_repo):
        """Test getting HEAD commit hash."""
        from gerrit_cli.git_utils import get_current_commit

        commit = get_current_commit()

        # Should be a 40-char hex string
        assert commit is not None
        assert len(commit) == 40
        assert all(c in '0123456789abcdef' for c in commit)

    def test_commit_exists(self, git_repo):
        """Test commit existence check."""
        from gerrit_cli.git_utils import commit_exists, get_current_commit

        head = get_current_commit()

        assert commit_exists(head) is True
        # Use a ref that clearly doesn't exist - not a valid hex hash
        assert commit_exists('refs/heads/nonexistent-branch-12345') is False

    def test_checkout(self, git_repo):
        """Test checkout."""
        import subprocess

        from gerrit_cli.git_utils import checkout, get_current_branch

        # Get initial branch name
        initial_branch = get_current_branch()

        # Create a new branch
        subprocess.run(
            ['git', 'checkout', '-b', 'test-branch'],
            cwd=git_repo, check=True, capture_output=True
        )

        assert get_current_branch() == 'test-branch'

        # Checkout back to initial branch
        success, msg = checkout(initial_branch)
        assert success is True
        assert get_current_branch() == initial_branch


class TestStagedParserHandlerContract:
    """Test the staged subcommand parser-handler contract."""

    def _create_full_parser(self):
        """Create a fully configured parser like main() does."""
        from gerrit_cli import cli
        from gerrit_cli.parsers import setup_parsers

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        # Map command names to handler functions (same as main())
        handlers = {
            'comments': cli.cmd_extract,
            'reply': cli.cmd_reply,
            'batch': cli.cmd_batch_reply,
            'review': cli.cmd_review,
            'series_comments': cli.cmd_series_comments,
            'series': cli.cmd_series,
            'series_status': cli.cmd_series_status,
            'interactive': cli.cmd_interactive,
            'work_on_patch': cli.cmd_work_on_patch,
            'next_patch': cli.cmd_next_patch,
            'finish_patch': cli.cmd_finish_patch,
            'abort': cli.cmd_abort,
            'status': cli.cmd_status,
            'stage': cli.cmd_stage,
            'push': cli.cmd_push,
            'staged_list': cli.cmd_staged_list,
            'staged_show': cli.cmd_staged_show,
            'staged_remove': cli.cmd_staged_remove,
            'staged_clear': cli.cmd_staged_clear,
            'staged_refresh': cli.cmd_staged_refresh,
            'continue_reintegration': cli.cmd_continue_reintegration,
            'skip_reintegration': cli.cmd_skip_reintegration,
            'reviewers': cli.cmd_reviewers,
            'add_reviewer': cli.cmd_add_reviewer,
            'remove_reviewer': cli.cmd_remove_reviewer,
            'find_user': cli.cmd_find_user,
            'abandon': cli.cmd_abandon,
            'checkout': cli.cmd_checkout,
            'maloo': cli.cmd_maloo,
            'info': cli.cmd_info,
            'series_info': cli.cmd_series_info,
            'watch': cli.cmd_watch,
            'set_topic': cli.cmd_set_topic,
            'hashtag': cli.cmd_hashtag,
            'related': cli.cmd_related,
            'restore': cli.cmd_restore,
            'rebase': cli.cmd_rebase,
            'vote': cli.cmd_vote,
            'diff': cli.cmd_diff,
            'message': cli.cmd_message,
            'search': cli.cmd_search,
            'explain': cli.cmd_explain,
            'examples': cli.cmd_examples,
            'done': cli.cmd_done,
            'ack': cli.cmd_ack,
            'describe': cli.cmd_describe,
        }

        setup_parsers(subparsers, handlers)
        return parser

    def test_staged_list_attributes(self):
        """Test staged list has required attributes."""
        parser = self._create_full_parser()
        args = parser.parse_args(['staged', 'list'])

        # Should be able to call the handler without AttributeError
        # We mock the actual staging manager
        with patch('gerrit_cli.cli.StagingManager') as MockStaging:
            MockStaging.return_value.list_all_staged.return_value = []
            with pytest.raises(SystemExit) as exc_info:
                args.func(args)
            assert exc_info.value.code == 0

    def test_staged_show_attributes(self):
        """Test staged show has required attributes."""
        parser = self._create_full_parser()
        args = parser.parse_args(['staged', 'show', '12345'])

        # Verify the handler can access change_number
        assert hasattr(args, 'change_number')
        assert args.change_number == 12345

    def test_staged_refresh_attributes(self):
        """Test staged refresh has required attributes."""
        parser = self._create_full_parser()
        args = parser.parse_args(['staged', 'refresh', '12345'])

        # This was the bug - handler expected change_number but parser had url
        assert hasattr(args, 'change_number')
        assert args.change_number == 12345

