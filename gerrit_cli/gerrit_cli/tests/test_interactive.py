"""Tests for interactive.py module."""

from unittest.mock import MagicMock, patch

from gerrit_cli.interactive import InteractiveSession
from gerrit_cli.models import ChangeInfo, CommentThread, ExtractedComments


class TestInteractiveActionHandlers:
    """Tests for action handler methods."""

    def setup_method(self):
        """Set up test fixtures."""
        self.session = InteractiveSession()
        # Create mock context
        self.mock_extracted = MagicMock(spec=ExtractedComments)
        self.mock_extracted.threads = [MagicMock(spec=CommentThread)]
        self.mock_extracted.change_info = MagicMock(spec=ChangeInfo)
        self.mock_extracted.change_info.current_patchset = 1
        self.mock_extracted.change_info.url = "https://example.com/12345"

        self.context = {
            'extracted': self.mock_extracted,
            'thread_index': 0,
            'change_number': 12345,
            'location': 'test.c:42',
        }

    @patch.object(InteractiveSession, '_stage_reply')
    @patch('builtins.input', return_value='')
    def test_action_done_default_message(self, mock_input, mock_stage):
        """Test done action with default message."""
        result_type, msg = self.session._action_done(self.context)

        assert result_type == 'break'
        assert self.session.staged_count == 1
        mock_stage.assert_called_once()
        # Check the message argument (4th positional arg)
        args = mock_stage.call_args[0]
        assert args[3] == 'Done'  # message
        assert args[4] is True   # resolve

    @patch.object(InteractiveSession, '_stage_reply')
    @patch('builtins.input', return_value='Fixed the issue')
    def test_action_done_custom_message(self, mock_input, mock_stage):
        """Test done action with custom message."""
        result_type, msg = self.session._action_done(self.context)

        assert result_type == 'break'
        args = mock_stage.call_args[0]
        assert args[3] == 'Fixed the issue'

    @patch.object(InteractiveSession, '_stage_reply')
    @patch('builtins.input', side_effect=['My reply', 'y'])
    def test_action_reply_with_resolve(self, mock_input, mock_stage):
        """Test reply action with resolve."""
        result_type, msg = self.session._action_reply(self.context)

        assert result_type == 'break'
        assert self.session.staged_count == 1
        args = mock_stage.call_args[0]
        assert args[3] == 'My reply'
        assert args[4] is True  # resolved

    @patch.object(InteractiveSession, '_stage_reply')
    @patch('builtins.input', side_effect=['My reply', 'n'])
    def test_action_reply_without_resolve(self, mock_input, mock_stage):
        """Test reply action without resolve."""
        result_type, msg = self.session._action_reply(self.context)

        assert result_type == 'break'
        args = mock_stage.call_args[0]
        assert args[4] is False  # not resolved

    @patch('builtins.input', return_value='')
    def test_action_reply_cancelled(self, mock_input):
        """Test reply action cancelled with empty input."""
        result_type, msg = self.session._action_reply(self.context)

        assert result_type == 'continue'
        assert self.session.staged_count == 0

    @patch.object(InteractiveSession, '_stage_reply')
    @patch('builtins.input', return_value='')
    def test_action_ack_default_message(self, mock_input, mock_stage):
        """Test ack action with default message."""
        result_type, msg = self.session._action_ack(self.context)

        assert result_type == 'break'
        assert self.session.staged_count == 1
        args = mock_stage.call_args[0]
        assert args[3] == 'Acknowledged'
        assert args[4] is True

    def test_action_skip(self):
        """Test skip action."""
        result_type, msg = self.session._action_skip(self.context)

        assert result_type == 'break'
        assert self.session.skipped_count == 1

    @patch('gerrit_cli.interactive.work_on_patch')
    def test_action_edit_success(self, mock_work):
        """Test edit action when work_on_patch succeeds."""
        mock_work.return_value = (True, "Started editing")

        result_type, msg = self.session._action_edit(self.context)

        assert result_type == 'exit'
        # _action_edit passes through whatever URL the change carries, and this
        # fixture's change_info.url is example.com (set in setUp). Asserting a
        # whamcloud URL contradicted the fixture rather than the behaviour.
        mock_work.assert_called_once_with(
            self.mock_extracted.change_info.url, 12345
        )

    @patch('gerrit_cli.interactive.work_on_patch')
    def test_action_edit_failure(self, mock_work):
        """Test edit action when work_on_patch fails."""
        mock_work.return_value = (False, "Failed to start")

        result_type, msg = self.session._action_edit(self.context)

        assert result_type == 'continue'

    @patch.object(InteractiveSession, '_push_all')
    def test_action_push(self, mock_push):
        """Test push action."""
        result_type, msg = self.session._action_push(self.context)

        assert result_type == 'continue'
        mock_push.assert_called_once()

    def test_action_quit(self):
        """Test quit action."""
        result_type, msg = self.session._action_quit(self.context)

        assert result_type == 'quit'

    def test_handle_action_invalid(self):
        """Test handling invalid action."""
        result_type, msg = self.session._handle_action('x', self.context)

        assert result_type == 'continue'

    def test_handle_action_dispatches(self):
        """Test that handle_action dispatches to correct handler."""
        with patch.object(self.session, '_action_skip') as mock_skip:
            mock_skip.return_value = ('break', None)
            result_type, msg = self.session._handle_action('s', self.context)

            mock_skip.assert_called_once_with(self.context)
            assert result_type == 'break'

