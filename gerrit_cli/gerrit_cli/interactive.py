"""Interactive mode for reviewing and responding to comments."""

import sys
from typing import Optional

from .extractor import extract_comments
from .models import ExtractedComments
from .rebase import work_on_patch
from .replier import CommentReplier
from .series import SeriesFinder
from .staging import StagingManager

# Action result types:
# - 'break': exit the action loop, move to next comment
# - 'continue': stay in action loop, re-prompt
# - 'quit': exit the entire session
# - 'exit': exit the program immediately (for edit mode)
ActionResult = tuple[str, Optional[str]]  # (result_type, message)


class InteractiveSession:
    """Interactive session for reviewing comments in a series."""

    def __init__(self):
        self.staging_manager = StagingManager()
        self.series_finder = SeriesFinder()
        self.replier = CommentReplier()
        self.staged_count = 0
        self.skipped_count = 0
        self.total_comments = 0

    def run_series(self, url: str):
        """Run interactive session for a patch series.

        Args:
            url: URL to any patch in the series
        """
        # Find all patches in series
        print("Finding patches in series...")
        series = self.series_finder.find_series(url)

        if not series or not series.patches:
            print("Error: Could not find series")
            return

        print(f"\nFound {len(series.patches)} patch(es) in series\n")

        # Collect all comments from all patches
        all_comments = []
        for patch in series.patches:
            try:
                extracted = extract_comments(
                    url=patch.url,
                    include_resolved=False,
                    include_code_context=True,
                )
                if extracted.threads:
                    for thread in extracted.threads:
                        all_comments.append({
                            'patch': patch,
                            'extracted': extracted,
                            'thread': thread,
                            'change_number': patch.change_number,
                        })
            except Exception as e:
                print(f"Warning: Could not fetch comments for {patch.change_number}: {e}")
                continue

        if not all_comments:
            print("No unresolved comments found in series!")
            return

        self.total_comments = len(all_comments)
        print(f"Found {self.total_comments} unresolved comment(s) across series\n")
        print("=" * 70)

        # Process each comment interactively
        for idx, comment_data in enumerate(all_comments, 1):
            self._process_comment(comment_data, idx)

        # Show summary and offer to push
        self._show_summary_and_push()

    def _process_comment(self, comment_data: dict, comment_num: int):
        """Process a single comment interactively.

        Args:
            comment_data: Dict with patch, extracted, thread info
            comment_num: Current comment number (1-indexed)
        """
        patch = comment_data['patch']
        thread = comment_data['thread']
        extracted = comment_data['extracted']
        change_number = comment_data['change_number']

        # Find thread index in the extracted comments
        thread_index = extracted.threads.index(thread)

        # Display comment
        print(f"\n[{comment_num}/{self.total_comments}] Change {change_number}: {patch.subject}")
        change_url = extracted.change_info.url
        print(f"Change URL: {change_url}")
        print("-" * 70)

        root = thread.root_comment
        location = f"{root.file_path}:{root.line or 'patchset'}"

        # Build comment URL from the change URL
        comment_url = f"{change_url}/comment/{root.id}/"

        print(f"Location: {location}")
        print(f"Comment URL: {comment_url}")
        print(f"Author: {root.author}")
        print("\nOriginal Comment:")
        print(f"  {root.message}")

        # Show code context if available
        if thread.code_context:
            print("\nCode context:")
            for line in thread.code_context.split('\n')[:5]:  # Show first 5 lines
                print(f"  {line}")
            if len(thread.code_context.split('\n')) > 5:
                print("  ...")

        # Show all replies in the thread
        if thread.replies:
            print(f"\nThread history ({len(thread.replies)} reply/replies):")
            for idx, reply in enumerate(thread.replies, 1):
                print(f"\n  Reply {idx} by {reply.author}:")
                # Indent the message
                for line in reply.message.split('\n'):
                    print(f"    {line}")

        # Get user action
        context = {
            'extracted': extracted,
            'thread_index': thread_index,
            'change_number': change_number,
            'location': location,
        }

        while True:
            print("\n" + "=" * 70)
            print("Actions: [d]one | [r]eply | [a]ck | [s]kip | [e]dit | [p]ush | [q]uit")
            action = input("Choose action: ").strip().lower()

            result_type, msg = self._handle_action(action, context)

            if result_type == 'break':
                break
            elif result_type == 'continue':
                continue
            elif result_type == 'quit':
                self._show_summary_and_push()
                sys.exit(0)
            elif result_type == 'exit':
                sys.exit(0)

    def _handle_action(self, action: str, context: dict) -> ActionResult:
        """Handle a single user action.

        Args:
            action: The action key (d, r, a, s, e, p, q)
            context: Dict with extracted, thread_index, change_number, location

        Returns:
            ActionResult tuple of (result_type, message)
        """
        handlers = {
            'd': self._action_done,
            'r': self._action_reply,
            'a': self._action_ack,
            's': self._action_skip,
            'e': self._action_edit,
            'p': self._action_push,
            'q': self._action_quit,
        }

        handler = handlers.get(action)
        if handler:
            return handler(context)
        else:
            print("Invalid action. Please choose d, r, a, s, e, p, or q.")
            return ('continue', None)

    def _action_done(self, context: dict) -> ActionResult:
        """Handle 'done' action - mark comment as done."""
        message = input("Message (press Enter for 'Done'): ").strip()
        if not message:
            message = "Done"
        self._stage_reply(
            context['extracted'], context['thread_index'],
            context['change_number'], message, True
        )
        print(f"✓ Staged 'Done' for {context['location']}")
        self.staged_count += 1
        return ('break', None)

    def _action_reply(self, context: dict) -> ActionResult:
        """Handle 'reply' action - add a custom reply."""
        print("Enter your reply (empty line to cancel):")
        message = input("> ").strip()
        if message:
            resolve = input("Mark as resolved? [y/N]: ").strip().lower() == 'y'
            self._stage_reply(
                context['extracted'], context['thread_index'],
                context['change_number'], message, resolve
            )
            print(f"✓ Staged reply for {context['location']}")
            self.staged_count += 1
            return ('break', None)
        else:
            print("Cancelled")
            return ('continue', None)

    def _action_ack(self, context: dict) -> ActionResult:
        """Handle 'ack' action - acknowledge the comment."""
        message = input("Message (press Enter for 'Acknowledged'): ").strip()
        if not message:
            message = "Acknowledged"
        self._stage_reply(
            context['extracted'], context['thread_index'],
            context['change_number'], message, True
        )
        print(f"✓ Staged acknowledgment for {context['location']}")
        self.staged_count += 1
        return ('break', None)

    def _action_skip(self, context: dict) -> ActionResult:
        """Handle 'skip' action - skip this comment."""
        print(f"⊘ Skipped {context['location']}")
        self.skipped_count += 1
        return ('break', None)

    def _action_edit(self, context: dict) -> ActionResult:
        """Handle 'edit' action - start edit mode for the patch."""
        change_number = context['change_number']
        print(f"\n🔧 Starting edit mode for patch {change_number}...")
        print("=" * 70)

        series_url = context['extracted'].change_info.url
        success, message = work_on_patch(series_url, change_number)
        print(message)

        if success:
            print("\n" + "=" * 70)
            print("⚠ EDIT MODE ACTIVE")
            print("=" * 70)
            print("")
            print("You are now in edit mode. The interactive session has been paused.")
            print("After you finish editing:")
            print("  1. Run: gerrit finish-patch")
            print("  2. Re-run: gerrit interactive <url>")
            print("")
            print("To abort the edit:")
            print("  Run: gerrit abort")
            print("")
            print("=" * 70)
            print("\nExiting interactive mode...")
            return ('exit', None)
        else:
            print("\n✗ Failed to start edit mode. Returning to comment review...")
            return ('continue', None)

    def _action_push(self, context: dict) -> ActionResult:
        """Handle 'push' action - push all staged operations now."""
        print("\n" + "=" * 70)
        print("Pushing staged operations now...")
        self._push_all()
        print("\nReturning to comment review...")
        return ('continue', None)

    def _action_quit(self, context: dict) -> ActionResult:
        """Handle 'quit' action - exit the session."""
        print("\nQuitting interactive session...")
        return ('quit', None)

    def _stage_reply(
        self,
        extracted: ExtractedComments,
        thread_index: int,
        change_number: int,
        message: str,
        resolve: bool,
    ):
        """Stage a reply to a comment thread."""
        thread = extracted.threads[thread_index]
        last_comment = thread.replies[-1] if thread.replies else thread.root_comment

        self.staging_manager.stage_operation(
            change_number=change_number,
            thread_index=thread_index,
            file_path=last_comment.file_path,
            line=last_comment.line,
            message=message,
            resolve=resolve,
            comment_id=last_comment.id,
            patchset=extracted.change_info.current_patchset,
            change_url=extracted.change_info.url,
        )

    def _show_summary_and_push(self):
        """Show session summary and offer to push staged operations."""
        print("\n" + "=" * 70)
        print("Session Summary")
        print("=" * 70)
        print(f"Total comments reviewed: {self.total_comments}")
        print(f"Staged operations: {self.staged_count}")
        print(f"Skipped: {self.skipped_count}")

        # Check what's staged
        staged_patches = self.staging_manager.list_all_staged()
        if not staged_patches:
            print("\nNo operations staged.")
            return

        print(f"\nStaged operations across {len(staged_patches)} patch(es):")
        for patch in staged_patches:
            print(f"  Change {patch.change_number}: {len(patch.operations)} operation(s)")

        # Offer to push
        push = input("\nPush all staged operations now? [y/N]: ").strip().lower()
        if push == 'y':
            self._push_all()
        else:
            print("\nStaged operations saved. You can push later with:")
            print("  gerrit staged list")
            for patch in staged_patches:
                print(f"  gerrit push {patch.change_number}")

    def _push_all(self):
        """Push all staged operations."""
        staged_patches = self.staging_manager.list_all_staged()

        if not staged_patches:
            print("No operations to push.")
            return

        print(f"\nPushing operations for {len(staged_patches)} patch(es)...")
        print("-" * 70)

        success_count = 0
        fail_count = 0

        for idx, patch in enumerate(staged_patches, 1):
            print(f"\n[{idx}/{len(staged_patches)}] Change {patch.change_number}:")
            success, message, count = self.replier.push_staged(
                change_number=patch.change_number,
                dry_run=False,
            )

            if success:
                print(f"  {message}")
                success_count += 1
            else:
                print(f"  ✗ Failed: {message}")
                fail_count += 1

        print("\n" + "=" * 70)
        print(f"Push complete: {success_count} succeeded, {fail_count} failed")
        print("=" * 70)


def run_interactive(url: str):
    """Run interactive session for a series.

    Args:
        url: URL to any patch in the series
    """
    session = InteractiveSession()
    session.run_series(url)
