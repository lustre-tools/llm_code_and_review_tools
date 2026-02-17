"""Integrated git rebase mode for working on patches with comments."""

import subprocess
from pathlib import Path
from typing import Optional

from . import git_utils
from .client import GerritCommentsClient
from .extractor import extract_comments
from .reintegration import ReintegrationManager, ReintegrationState
from .replier import CommentReplier
from .series import PatchInfo, SeriesFinder
from .session import RebaseSession, SessionManager


class RebaseManager:
    """Manages git rebase operations for working on patches."""

    def __init__(self, session_manager: SessionManager | None = None):
        self.series_finder = SeriesFinder()
        self.client = GerritCommentsClient()
        self._session_mgr = session_manager or SessionManager()
        self._git = git_utils.GitRunner()
        self._reintegration = ReintegrationManager(self._git)

    @property
    def state_file(self) -> Path:
        """For backwards compatibility."""
        return self._session_mgr.state_file

    def _run_git(self, args: list[str], capture_output: bool = True, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command. Delegates to GitRunner."""
        return self._git.run(args, capture_output=capture_output, check=check)

    def check_git_repo(self) -> tuple[bool, str]:
        """Check if we're in a git repository with clean state."""
        is_repo, msg = git_utils.check_git_repo()
        if not is_repo:
            return False, "Not in a git repository. Please run from within a git repository."
        return git_utils.is_working_tree_clean()

    def get_current_branch(self) -> Optional[str]:
        """Get the current branch name."""
        return git_utils.get_current_branch()

    def get_current_commit(self) -> Optional[str]:
        """Get the current commit hash."""
        return git_utils.get_current_commit()

    def _is_cherry_pick_in_progress(self) -> bool:
        """Check if a cherry-pick operation is in progress."""
        return git_utils.is_cherry_pick_in_progress()

    def _has_unmerged_files(self) -> bool:
        """Check if there are unmerged files (unresolved conflicts)."""
        return git_utils.has_unmerged_files()

    def _get_gerrit_remote(self) -> str:
        """Get the name of the Gerrit remote.

        Looks for a remote whose URL contains a Gerrit hostname
        (review.whamcloud.com or review.gerrithub.io or similar).
        Falls back to 'origin' if none found.

        Returns:
            Remote name to use for Gerrit fetches
        """
        try:
            result = self._run_git(["remote", "-v"])
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    name, url = parts[0], parts[1]
                    if "review." in url or "gerrit" in url.lower():
                        return name
        except Exception:
            pass
        return "origin"

    def _fetch_gerrit_commit(self, change_number: int) -> tuple[bool, str]:
        """Fetch a specific change from Gerrit.

        Fetches the latest patchset of a change using refs/changes/XX/NNNNN/PS.
        Tries the Gerrit remote first (any remote with a gerrit-like URL),
        then falls back to 'origin'.

        Args:
            change_number: The Gerrit change number

        Returns:
            Tuple of (success, message or commit hash)
        """
        try:
            # Get the current patchset number from Gerrit API
            client = GerritCommentsClient()
            change = client.get_change_detail(change_number)
            current_revision = change.get("current_revision", "")
            revisions = change.get("revisions", {})
            patchset = revisions.get(current_revision, {}).get("_number")

            if not patchset:
                return False, f"Could not determine current patchset for change {change_number}"

            # Gerrit refs are stored as refs/changes/XX/NNNNN/PS
            # where XX is the last 2 digits of the change number
            suffix = str(change_number)[-2:].zfill(2)
            ref = f"refs/changes/{suffix}/{change_number}/{patchset}"

            # Try the Gerrit remote first, fall back to origin
            remote = self._get_gerrit_remote()
            try:
                self._run_git(["fetch", remote, ref])
            except subprocess.CalledProcessError:
                if remote != "origin":
                    self._run_git(["fetch", "origin", ref])

            # FETCH_HEAD now points to the fetched commit
            result = self._run_git(["rev-parse", "FETCH_HEAD"])
            commit = result.stdout.strip()

            return True, commit
        except subprocess.CalledProcessError as e:
            return False, f"Failed to fetch change {change_number}: {e}"

    def _start_reintegration(
        self,
        url: str,
        target_change: int,
        series,  # PatchSeries
    ) -> tuple[bool, str]:
        """Start reintegration of stale patches into the series.

        When patches in a series have newer patchsets that are still connected
        to the series, we need to rebase the descendants onto the newer versions.

        Args:
            url: URL to any patch in the series
            target_change: The change number the user wants to work on
            series: PatchSeries with stale_info populated

        Returns:
            Tuple of (success, message)
        """
        from datetime import datetime

        # Get current state for restoration later
        original_head = self.get_current_commit()
        original_branch = self.get_current_branch()

        if not original_head:
            return False, "Could not determine current commit"

        # Build list of changes in the series (base to tip order)
        series_patches = [{
            "change_number": p.change_number,
            "subject": p.subject,
            "commit": p.commit,
        } for p in series.patches]

        # Use ReintegrationManager to create state
        reint_state, descendants = self._reintegration.create_state(
            series.stale_info, series_patches
        )

        # Get first stale change info
        first_stale = series.stale_info[0]
        first_stale_change = first_stale.change_number

        # Print reintegration info
        msg = self._reintegration.format_start_message(
            first_stale_change,
            first_stale.old_revision,
            first_stale.current_revision,
            first_stale.subject,
            descendants,
            series_patches,
        )
        print(msg)

        # Fetch and checkout the newer patchset
        print(f"\nFetching change {first_stale_change} from Gerrit...")
        success, result = self._fetch_gerrit_commit(first_stale_change)
        if not success:
            return False, f"Failed to fetch stale change: {result}"

        new_commit = result
        print(f"Fetched commit: {new_commit[:12]}")

        # Checkout the new commit
        try:
            self._run_git(["checkout", new_commit])
        except subprocess.CalledProcessError as e:
            return False, f"Failed to checkout new commit: {e}"

        # Update the series patches with the new commit for the stale change
        for p in series_patches:
            if p['change_number'] == first_stale_change:
                p['commit'] = new_commit[:12]
                break

        # Save session with reintegration state
        session = RebaseSession(
            series_url=url,
            target_change=target_change,
            target_commit=new_commit,
            original_head=original_head,
            original_branch=original_branch or "HEAD",
            series_patches=series_patches,
            started_at=datetime.now().isoformat(),
            reintegration=reint_state.to_dict(),
        )
        self.save_session(session)

        # Start cherry-picking if there are changes to rebase
        if descendants:
            return self._continue_reintegration(session)
        else:
            # No descendants to rebase - check for more stale changes
            return self._advance_reintegration(session)

    def _continue_reintegration(
        self,
        session: RebaseSession
    ) -> tuple[bool, str]:
        """Continue the reintegration process by cherry-picking next change.

        Args:
            session: The current rebase session

        Returns:
            Tuple of (success, message)
        """
        reint_state = session.reintegration_state
        next_change = self._reintegration.get_next_descendant(reint_state)

        if next_change is None:
            # All changes cherry-picked, advance to next stale change
            return self._advance_reintegration(session)

        # Find the commit for this change
        patch_info = next(
            (p for p in session.series_patches if p['change_number'] == next_change),
            None
        )
        if not patch_info:
            return False, f"Could not find patch info for change {next_change}"

        commit = patch_info.get('commit')
        if not commit:
            return False, f"No commit hash for change {next_change}"

        # Try to fetch and find the actual commit
        # First, try to find it locally
        try:
            result = self._run_git(["rev-parse", "--verify", commit], check=False)
            if result.returncode != 0:
                # Need to fetch from Gerrit
                print(f"Fetching change {next_change} from Gerrit...")
                success, fetched = self._fetch_gerrit_commit(next_change)
                if not success:
                    return False, f"Failed to fetch change {next_change}: {fetched}"
                commit = fetched
        except Exception:
            pass

        print(f"\nCherry-picking change {next_change} ({patch_info.get('subject', '')[:50]})")

        # Set pending cherry-pick state
        session.pending_cherry_pick = next_change
        self.save_session(session)

        # Try to cherry-pick
        try:
            self._run_git(["cherry-pick", commit])
            # Success! Update session and continue
            return self._reintegration_cherry_pick_success(session, next_change)
        except subprocess.CalledProcessError:
            # Check for conflicts vs empty commit
            if self._is_cherry_pick_in_progress():
                # Conflicts - user needs to resolve
                return True, self._format_reintegration_conflict_message(session, next_change)
            else:
                # Empty commit or other issue - skip
                print("  Commit appears to be empty or already applied, skipping...")
                try:
                    self._run_git(["cherry-pick", "--abort"], check=False)
                except Exception:
                    pass
                return self._reintegration_skip_change(session, next_change)

    def _reintegration_cherry_pick_success(
        self,
        session: RebaseSession,
        change_number: int
    ) -> tuple[bool, str]:
        """Handle successful cherry-pick during reintegration."""
        # Get the new commit hash
        new_commit = self.get_current_commit()

        # Update reintegration state via manager
        reint_state = session.reintegration_state
        self._reintegration.mark_descendant_done(
            reint_state, change_number, session.rebased_changes
        )
        session.update_reintegration(reint_state)
        session.pending_cherry_pick = None

        # Update the patch commit in session
        for p in session.series_patches:
            if p['change_number'] == change_number:
                p['commit'] = new_commit[:12] if new_commit else p['commit']
                break

        self.save_session(session)

        print(f"  ✓ Successfully cherry-picked change {change_number}")

        # Continue with next change
        return self._continue_reintegration(session)

    def _reintegration_skip_change(
        self,
        session: RebaseSession,
        change_number: int
    ) -> tuple[bool, str]:
        """Skip a change during reintegration."""
        reint_state = session.reintegration_state
        self._reintegration.mark_descendant_skipped(
            reint_state, change_number, session.skipped_changes
        )
        session.update_reintegration(reint_state)
        session.pending_cherry_pick = None
        self.save_session(session)

        # Continue with next change
        return self._continue_reintegration(session)

    def _advance_reintegration(
        self,
        session: RebaseSession
    ) -> tuple[bool, str]:
        """Advance to the next stale change or complete reintegration."""
        reint_state = session.reintegration_state

        # Use manager to advance state
        has_more = self._reintegration.advance_to_next_stale(
            reint_state, session.series_patches
        )
        session.update_reintegration(reint_state)

        if not has_more:
            # All stale changes processed - reintegration complete!
            return self._complete_reintegration(session)

        # Get next stale change info
        next_stale = reint_state.current_stale
        if next_stale is None:
            # Shouldn't happen if has_more is True, but be safe
            return self._complete_reintegration(session)
        next_stale_change = next_stale['change_number']

        # Fetch and checkout the newer patchset
        print(f"\n{'='*70}")
        print(f"REINTEGRATING CHANGE {next_stale_change}")
        print(f"{'='*70}")
        print(f"\nChange {next_stale_change} has a newer patchset "
              f"(v{next_stale['old_revision']} -> v{next_stale['current_revision']}).")
        print(f"Subject: {next_stale.get('subject', 'Unknown')}")

        print(f"\nFetching change {next_stale_change} from Gerrit...")
        success, result = self._fetch_gerrit_commit(next_stale_change)
        if not success:
            return False, f"Failed to fetch stale change: {result}"

        new_commit = result
        print(f"Fetched commit: {new_commit[:12]}")

        # Checkout the new commit
        try:
            self._run_git(["checkout", new_commit])
        except subprocess.CalledProcessError as e:
            return False, f"Failed to checkout new commit: {e}"

        # Update session
        for p in session.series_patches:
            if p['change_number'] == next_stale_change:
                p['commit'] = new_commit[:12]
                break

        self.save_session(session)

        # Continue cherry-picking
        if reint_state.pending_descendants:
            return self._continue_reintegration(session)
        else:
            return self._advance_reintegration(session)

    def _complete_reintegration(
        self,
        session: RebaseSession
    ) -> tuple[bool, str]:
        """Complete the reintegration process and transition to normal review."""
        # Clear reintegration state
        session.update_reintegration(ReintegrationState())
        self.save_session(session)

        # Format completion message
        msg = self._reintegration.format_complete_message(
            len(session.rebased_changes),
            len(session.skipped_changes),
        )
        print(msg)

        lines = [
            "\nThe series is now consistent. You can continue with:",
            f"  gerrit review-series {session.series_url}",
        ]

        return True, "\n".join(lines)

    def _format_reintegration_conflict_message(
        self,
        session: RebaseSession,
        change_number: int
    ) -> str:
        """Format message for cherry-pick conflict during reintegration."""
        patch = next(
            (p for p in session.series_patches if p['change_number'] == change_number),
            None
        )
        subject = patch.get('subject', 'Unknown') if patch else 'Unknown'

        return self._reintegration.format_conflict_message(change_number, subject)

    def continue_reintegration(self) -> tuple[bool, str]:
        """Continue reintegration after conflict resolution.

        Returns:
            Tuple of (success, message)
        """
        session = self.load_session()
        if not session:
            return False, "No active session"

        if not session.reintegrating:
            return False, "Not in reintegration mode"

        # Check if cherry-pick is still in progress
        if self._is_cherry_pick_in_progress():
            return False, "Cherry-pick still in progress. Resolve conflicts and run 'git cherry-pick --continue' first."

        if self._has_unmerged_files():
            return False, "There are still unmerged files. Resolve all conflicts first."

        # Cherry-pick completed successfully
        if session.pending_cherry_pick:
            return self._reintegration_cherry_pick_success(session, session.pending_cherry_pick)
        else:
            return self._continue_reintegration(session)

    def skip_reintegration(self) -> tuple[bool, str]:
        """Skip the current change during reintegration.

        Returns:
            Tuple of (success, message)
        """
        session = self.load_session()
        if not session:
            return False, "No active session"

        if not session.reintegrating:
            return False, "Not in reintegration mode"

        if not session.pending_cherry_pick:
            return False, "No pending cherry-pick to skip"

        # Abort any in-progress cherry-pick
        try:
            self._run_git(["cherry-pick", "--abort"], check=False)
        except Exception:
            pass

        return self._reintegration_skip_change(session, session.pending_cherry_pick)

    def _is_change_id_in_history(self, change_id: str, since_commit: str) -> bool:
        """Check if a Change-Id exists in commits since a given commit.

        Args:
            change_id: The Gerrit Change-Id to look for
            since_commit: The commit to start searching from (exclusive)

        Returns:
            True if Change-Id is found in history after since_commit
        """
        try:
            # Get commits since the base commit
            result = self._run_git([
                "log", "--format=%B", f"{since_commit}..HEAD"
            ])
            return f"Change-Id: {change_id}" in result.stdout
        except subprocess.CalledProcessError:
            return False

    def find_commit_by_change_id(self, change_number: int, patches: list[PatchInfo]) -> Optional[str]:
        """Find the local commit hash for a Gerrit change.

        Args:
            change_number: Gerrit change number
            patches: List of patches in the series

        Returns:
            Commit hash or None if not found
        """
        # Find the patch info
        target_patch = None
        for patch in patches:
            if patch.change_number == change_number:
                target_patch = patch
                break

        if not target_patch:
            return None

        # Try to find commit with matching Change-Id in commit message
        try:
            # Get the Change-Id from Gerrit
            change_detail = self.client.get_change_detail(change_number)
            change_id = change_detail.get("change_id", "")

            if not change_id:
                return None

            # Search git log for this Change-Id
            result = self._run_git([
                "log",
                "--all",
                "--grep", f"Change-Id: {change_id}",
                "--format=%H",
                "-n", "1"
            ])

            commit = result.stdout.strip()
            return commit if commit else None

        except Exception:
            return None

    def start_rebase_to_patch(
        self,
        url: str,
        change_number: int,
    ) -> tuple[bool, str]:
        """Start or continue a rebase session to edit a specific patch.

        If there's an existing session for the same series, this will use
        the updated commit hashes from that session (after previous finish-patch
        operations have rebased descendants).

        Args:
            url: URL to any patch in the series
            change_number: The change number to work on

        Returns:
            Tuple of (success, message)
        """
        from datetime import datetime

        # Check git state (allow uncommitted if just switching patches)
        is_valid, msg = self.check_git_repo()
        if not is_valid:
            # Check if we're continuing in an existing session
            existing_session = self.load_session()
            if not existing_session:
                return False, msg
            # If there's an existing session, we might have uncommitted changes
            # from the previous patch - warn but allow
            print(f"⚠ Warning: {msg}")
            print("  Continuing with existing session...")

        # Check for existing session
        existing_session = self.load_session()

        if existing_session:
            # Use the existing session's patch info (with updated commits)
            series_patches = existing_session.series_patches
            original_head = existing_session.original_head
            original_branch = existing_session.original_branch

            # Find the target patch in the existing session
            target_patch_info = None
            target_index = -1
            for idx, p in enumerate(series_patches):
                if p['change_number'] == change_number:
                    target_patch_info = p
                    target_index = idx
                    break

            if not target_patch_info:
                return False, f"Change {change_number} not found in series"

            # Use the potentially-updated commit hash from the session
            commit_hash = target_patch_info.get('commit')
            if not commit_hash:
                # Fall back to looking it up
                commit_hash = self._find_commit_by_change_number(change_number)

            if not commit_hash:
                return False, f"Could not find commit for change {change_number}"

            # Create PatchInfo objects from session dicts
            base_url = existing_session.series_url.rsplit('/', 1)[0]
            target_patch = PatchInfo.from_dict(target_patch_info, base_url)
            patches = [PatchInfo.from_dict(p, base_url) for p in series_patches]

        else:
            # New session - find the series from Gerrit
            try:
                series = self.series_finder.find_series(url)
                patches = series.patches
            except Exception as e:
                return False, f"Error finding series: {e}"

            if not patches:
                return False, "Could not find series"

            # Check for stale changes (patches with newer patchsets)
            if series.error:
                if series.needs_reintegration:
                    # Stale changes can be reintegrated
                    return self._start_reintegration(url, change_number, series)
                else:
                    # Stale changes pulled out of series - warn but proceed
                    print(f"⚠ Warning: {series.error}")

            # Find the target patch
            target_patch = None
            target_index = -1
            for idx, patch in enumerate(patches):
                if patch.change_number == change_number:
                    target_patch = patch
                    target_index = idx
                    break

            if not target_patch:
                return False, f"Change {change_number} not found in series"

            # Find the commit hash for this change; if not local, fetch from Gerrit
            commit_hash = self.find_commit_by_change_id(change_number, patches)
            if not commit_hash:
                print(f"Change {change_number} not in local history, fetching from Gerrit...")
                success, result = self._fetch_gerrit_commit(change_number)
                if not success:
                    return False, result
                commit_hash = result

            # Get current state for restoration later
            head = self.get_current_commit()
            branch = self.get_current_branch()

            if not head:
                return False, "Could not determine current commit"
            original_head = head
            original_branch = branch or "HEAD"

            series_patches = [{
                "change_number": p.change_number,
                "subject": p.subject,
                "commit": p.commit,
            } for p in patches]

        # Try to checkout the target commit, fetching from Gerrit if needed
        try:
            self._run_git(["checkout", commit_hash])
        except subprocess.CalledProcessError:
            # Commit not found locally - fetch from Gerrit
            print(f"Commit {commit_hash[:12]} not found locally, fetching from Gerrit...")
            success, result = self._fetch_gerrit_commit(change_number)
            if not success:
                return False, result

            # Update commit_hash to the fetched one
            commit_hash = result
            try:
                self._run_git(["checkout", commit_hash])
            except subprocess.CalledProcessError as e:
                return False, f"Error checking out fetched commit {commit_hash}: {e}"

        # Save/update session state
        session = RebaseSession(
            series_url=url,
            target_change=change_number,
            target_commit=commit_hash,
            original_head=original_head,
            original_branch=original_branch or "HEAD",
            series_patches=series_patches,
            started_at=existing_session.started_at if existing_session else datetime.now().isoformat(),
        )
        self.save_session(session)

        # target_patch is guaranteed non-None at this point due to earlier checks
        assert target_patch is not None
        return True, self._format_work_on_instructions(target_patch, patches, target_index)

    def _format_work_on_instructions(
        self,
        target_patch: PatchInfo,
        series_patches: list[PatchInfo],
        target_index: int,
    ) -> str:
        """Format instructions for working on a patch."""
        lines = []
        lines.append("=" * 70)
        lines.append(f"📝 Working on Patch {target_index + 1}/{len(series_patches)}")
        lines.append("=" * 70)
        lines.append(f"Change: {target_patch.change_number}")
        lines.append(f"Subject: {target_patch.subject}")
        lines.append(f"URL: {target_patch.url}")
        lines.append("")

        # Show comments for this patch
        try:
            comments = extract_comments(
                url=target_patch.url,
                include_resolved=False,
                include_code_context=True,
            )

            if comments.threads:
                lines.append(f"📋 {len(comments.threads)} Unresolved Comment(s):")
                lines.append("-" * 70)
                for idx, thread in enumerate(comments.threads):
                    root = thread.root_comment
                    location = f"{root.file_path}:{root.line or 'patchset'}"
                    lines.append(f"\n[{idx}] {location}")
                    lines.append(f"    Author: {root.author}")
                    # Show code context if available
                    if root.code_context:
                        lines.append("    Code:")
                        for ctx_line in root.code_context.format().split('\n'):
                            lines.append(f"      {ctx_line}")
                    # Show comment message
                    lines.append(f"    Comment: {root.message}")
                    # Show any replies in thread
                    if thread.replies:
                        for reply in thread.replies:
                            lines.append(f"      └─ {reply.author}: {reply.message}")
                lines.append("")
                lines.append("💬 To reply to comments:")
                lines.append("  gerrit stage --done <index>     # Mark as done")
                lines.append("  gerrit stage <index> \"message\"  # Reply with message")
            else:
                lines.append("✓ No unresolved comments for this patch")
        except Exception as e:
            lines.append(f"⚠ Could not fetch comments: {e}")

        lines.append("")
        lines.append("=" * 70)
        lines.append("🛠 WORKFLOW")
        lines.append("=" * 70)
        lines.append("")
        lines.append("1. Edit files and stage replies")
        lines.append("2. git add <files> && git commit --amend --no-edit")
        lines.append("3. gerrit finish-patch  (rebases series, goes to next patch)")
        lines.append("")
        lines.append("When done with all patches: gerrit end-session")
        lines.append("To discard all changes:     gerrit abort-session")
        lines.append("")
        lines.append("=" * 70)

        return "\n".join(lines)

    def finish_rebase(self) -> tuple[bool, str]:
        """Finish the current rebase session.

        After the user has amended the target commit, this method:
        1. Gets the amended commit hash
        2. Cherry-picks all descendant patches onto the amended commit
        3. Updates the session with new commit mappings
        4. Returns to the new tip

        Returns:
            Tuple of (success, message)
        """
        session = self.load_session()
        if not session:
            return False, "No active rebase session"

        try:
            lines = []
            lines.append("=" * 70)

            # PHASE 1: Handle any in-progress cherry-pick FIRST
            # This must be done before we try to start new cherry-picks
            if self._is_cherry_pick_in_progress():
                if self._has_unmerged_files():
                    lines.append("⚠ Unresolved conflicts - please fix them first:")
                    lines.append("  1. Edit the conflicted files")
                    lines.append("  2. git add <resolved files>")
                    lines.append("  3. Run 'gerrit finish-patch' again")
                    lines.append("")
                    lines.append("Or to skip this commit: git cherry-pick --skip")
                    return False, "\n".join(lines)

                # Conflicts resolved, continue the cherry-pick
                try:
                    self._run_git(["cherry-pick", "--continue"])
                    if session.pending_cherry_pick:
                        session.rebased_changes.append(session.pending_cherry_pick)
                        session.pending_cherry_pick = None
                        self.save_session(session)
                    lines.append("✓ Cherry-pick continued successfully")
                except subprocess.CalledProcessError:
                    # Empty commit - auto-skip
                    lines.append("ℹ Cherry-pick resulted in empty commit, skipping...")
                    if session.pending_cherry_pick:
                        session.skipped_changes.append(session.pending_cherry_pick)
                        session.pending_cherry_pick = None
                        self.save_session(session)
                    try:
                        self._run_git(["cherry-pick", "--skip"])
                    except subprocess.CalledProcessError:
                        pass

            # PHASE 2: Check if user ran 'git cherry-pick --skip' manually
            elif session.pending_cherry_pick:
                skipped = session.pending_cherry_pick
                session.skipped_changes.append(skipped)
                session.pending_cherry_pick = None
                self.save_session(session)
                lines.append(f"ℹ Detected skip of change {skipped}")

            # PHASE 3: Now check git state again - a new conflict may have appeared
            if self._is_cherry_pick_in_progress():
                if self._has_unmerged_files():
                    lines.append("⚠ New conflict after continue - please resolve:")
                    lines.append("  1. Edit the conflicted files")
                    lines.append("  2. git add <resolved files>")
                    lines.append("  3. Run 'gerrit finish-patch' again")
                    return False, "\n".join(lines)

            # Get the current commit
            amended_commit = self.get_current_commit()
            if not amended_commit:
                return False, "Could not determine current commit"

            # Find the target patch index in the series
            target_index = -1
            for idx, patch in enumerate(session.series_patches):
                if patch['change_number'] == session.target_change:
                    target_index = idx
                    break

            if target_index == -1:
                return False, "Could not find target patch in series"

            # Get descendant patches (those after our target)
            descendants = session.series_patches[target_index + 1:]

            if amended_commit != session.target_commit:
                lines.append(f"✓ Commit amended: {session.target_commit[:8]} → {amended_commit[:8]}")
            else:
                lines.append("ℹ Commit unchanged")

            # PHASE 4: Cherry-pick remaining descendants
            if descendants:
                # Filter out already-rebased or skipped patches
                remaining = []
                for d in descendants:
                    change_num = d['change_number']
                    if change_num in session.rebased_changes:
                        continue
                    if change_num in session.skipped_changes:
                        continue
                    if change_num in session.skipped_changes:
                        continue
                    # Check if this Change-Id is already in our history
                    # (in case user ran git cherry-pick --continue manually)
                    change_id = self._get_change_id_for_change(change_num)
                    if change_id and self._is_change_id_in_history(change_id, amended_commit):
                        # Mark as rebased and skip
                        session.rebased_changes.append(change_num)
                        continue
                    remaining.append(d)

                if remaining:
                    lines.append(f"\n📦 Rebasing {len(remaining)} remaining patch(es)...")
                else:
                    lines.append(f"\n✓ All {len(descendants)} descendant patch(es) already rebased")

                # Track new commit hashes for descendants
                new_commits = {session.target_change: amended_commit}

                for desc in remaining:
                    change_num = desc['change_number']
                    # Get the original commit for this descendant
                    commit_to_pick = desc.get('commit')
                    if not commit_to_pick:
                        commit_to_pick = self._find_commit_by_change_number(change_num)

                    if not commit_to_pick:
                        return False, f"Could not find commit for change {change_num}"
                    original_commit: str = commit_to_pick

                    # Track that we're about to cherry-pick this
                    session.pending_cherry_pick = change_num
                    self.save_session(session)

                    # Cherry-pick this commit
                    try:
                        self._run_git(["cherry-pick", original_commit])
                        new_hash = self.get_current_commit()
                        if new_hash is None:
                            return False, f"Could not get commit hash after cherry-pick of {change_num}"
                        new_commits[change_num] = new_hash
                        session.rebased_changes.append(change_num)
                        session.pending_cherry_pick = None
                        self.save_session(session)  # Save progress after each success
                        lines.append(f"  ✓ {change_num}: {desc['subject'][:40]}...")
                    except subprocess.CalledProcessError:
                        # Check if this is an empty commit (no changes)
                        if not self._has_unmerged_files():
                            # Empty commit - auto-skip
                            lines.append(f"  ⊘ {change_num}: empty (already in upstream)")
                            session.skipped_changes.append(change_num)
                            session.pending_cherry_pick = None
                            self.save_session(session)
                            try:
                                self._run_git(["cherry-pick", "--skip"])
                            except subprocess.CalledProcessError:
                                pass
                            continue

                        # Real conflict
                        lines.append(f"\n⚠ CONFLICT cherry-picking {change_num}")
                        lines.append(f"  Subject: {desc['subject']}")
                        lines.append("")
                        lines.append("To resolve:")
                        lines.append("  1. Fix the conflicts in the files")
                        lines.append("  2. git add <resolved files>")
                        lines.append("  3. gerrit finish-patch")
                        lines.append("")
                        lines.append("To skip this commit: git cherry-pick --skip")
                        lines.append("To abort: gerrit abort-patch")

                        # Save progress (keep pending_cherry_pick set)
                        session.series_patches = self._update_commits_in_patches(
                            session.series_patches, new_commits
                        )
                        self.save_session(session)

                        return False, "\n".join(lines)

                # Update session with new commits and clear rebased tracking
                session.series_patches = self._update_commits_in_patches(
                    session.series_patches, new_commits
                )
                session.rebased_changes = []  # Clear for next patch
                self.save_session(session)

            # Get the new tip
            self.get_current_commit()

            # Push any staged replies for this patch
            replier = CommentReplier()
            push_success, push_msg, push_count = replier.push_staged(
                session.target_change, dry_run=False
            )

            lines.append("")
            if push_count > 0:
                if push_success:
                    lines.append(f"📤 Pushed {push_count} staged reply(ies) to Gerrit")
                else:
                    lines.append(f"⚠ Failed to push staged replies: {push_msg}")

            lines.append("")
            lines.append("=" * 70)
            lines.append("✓ Patch complete!")
            lines.append("")

            # Show push instruction for just the current patch
            if amended_commit != session.target_commit:
                lines.append("To push this patch:")
                lines.append(f"  git push origin {amended_commit}:refs/for/master")
                lines.append("")

            lines.append("Next: gerrit next-patch")
            lines.append("=" * 70)

            # DON'T clear session yet - keep it so work-on-patch can use the updated commits
            # Only clear when explicitly done or when starting fresh

        except subprocess.CalledProcessError as e:
            return False, f"Error completing rebase: {e}\n\nYou may need to resolve conflicts manually."

        return True, "\n".join(lines)

    def _get_change_id_for_change(self, change_number: int) -> Optional[str]:
        """Get the Change-Id for a given change number from Gerrit."""
        try:
            change_detail = self.client.get_change_detail(change_number)
            return change_detail.get("change_id", None)
        except Exception:
            return None

    def _find_commit_by_change_number(self, change_number: int) -> Optional[str]:
        """Find commit hash for a change number using Change-Id."""
        try:
            change_id = self._get_change_id_for_change(change_number)
            if not change_id:
                return None

            result = self._run_git([
                "log", "--all",
                "--grep", f"Change-Id: {change_id}",
                "--format=%H", "-n", "1"
            ])
            commit = result.stdout.strip()
            return commit if commit else None
        except Exception:
            return None

    def _update_commits_in_patches(
        self, patches: list[dict], new_commits: dict
    ) -> list[dict]:
        """Update commit hashes in patch list."""
        updated = []
        for patch in patches:
            p = patch.copy()
            if patch['change_number'] in new_commits:
                p['commit'] = new_commits[patch['change_number']]
            updated.append(p)
        return updated

    def _get_patch_commit(self, patches: list[dict], change_number: int) -> Optional[str]:
        """Get commit hash for a patch from the list."""
        for patch in patches:
            if patch['change_number'] == change_number:
                return patch.get('commit')
        return None

    def abort_rebase(self) -> tuple[bool, str]:
        """Abort the current rebase session.

        Returns:
            Tuple of (success, message)
        """
        session = self.load_session()
        if not session:
            return False, "No active rebase session"

        # Return to original state
        try:
            # Check if we're in a rebase
            try:
                self._run_git(["rev-parse", "--verify", "REBASE_HEAD"])
                # In rebase, abort it
                self._run_git(["rebase", "--abort"])
            except subprocess.CalledProcessError:
                # Not in rebase, just checkout original head
                pass

            # Return to original branch/head
            if session.original_branch and session.original_branch != "HEAD":
                self._run_git(["checkout", session.original_branch])
            else:
                self._run_git(["checkout", session.original_head])

        except subprocess.CalledProcessError as e:
            return False, f"Error aborting rebase: {e}"

        # Clear session
        self.clear_session()

        return True, "✓ Rebase aborted. Returned to original state."

    def get_status(self) -> tuple[bool, str]:
        """Get the current rebase session status.

        Returns:
            Tuple of (has_session, status_message)
        """
        session = self.load_session()
        if not session:
            return False, "No active rebase session"

        lines = []
        lines.append("=" * 70)
        lines.append("📊 REBASE SESSION STATUS")
        lines.append("=" * 70)
        lines.append(f"Target Change: {session.target_change}")
        lines.append(f"Series URL: {session.series_url}")
        lines.append(f"Started: {session.started_at}")
        lines.append(f"Original Branch: {session.original_branch}")
        lines.append("")

        # Check git state
        current_commit = self.get_current_commit()
        current_branch = self.get_current_branch()

        lines.append(f"Current Commit: {current_commit[:8] if current_commit else 'unknown'}")
        lines.append(f"Current Branch: {current_branch or 'detached HEAD'}")
        lines.append("")

        # Check if commit was modified
        if current_commit and current_commit != session.target_commit:
            lines.append("✓ Commit has been modified")
        else:
            lines.append("  No changes to commit yet")

        lines.append("")
        lines.append("Next steps:")
        lines.append("  gerrit finish-patch    # Complete the rebase")
        lines.append("  gerrit abort-patch     # Abort and return to original state")
        lines.append("=" * 70)

        return True, "\n".join(lines)

    def has_active_session(self) -> bool:
        """Check if there's an active rebase session."""
        return self._session_mgr.has_active_session()

    def save_session(self, session: RebaseSession):
        """Save rebase session state."""
        self._session_mgr.save(session)

    def load_session(self) -> Optional[RebaseSession]:
        """Load rebase session state."""
        return self._session_mgr.load()

    def clear_session(self):
        """Clear the saved session state."""
        self._session_mgr.clear()


def work_on_patch(url: str, change_number: int) -> tuple[bool, str]:
    """Start working on a specific patch in a series.

    Args:
        url: URL to any patch in the series
        change_number: The change number to work on

    Returns:
        Tuple of (success, message)
    """
    manager = RebaseManager()
    return manager.start_rebase_to_patch(url, change_number)


def finish_patch(auto_next: bool = True) -> tuple[bool, str]:
    """Finish working on the current patch and optionally advance.

    Args:
        auto_next: If True, automatically move to next patch with comments

    Returns:
        Tuple of (success, message)
    """
    manager = RebaseManager()
    success, message = manager.finish_rebase()

    if not success or not auto_next:
        return success, message

    # Try to auto-advance to next patch with comments
    session = manager.load_session()
    if not session:
        return success, message

    # Find current patch index
    current_index = -1
    for idx, patch in enumerate(session.series_patches):
        if patch['change_number'] == session.target_change:
            current_index = idx
            break

    if current_index == -1 or current_index >= len(session.series_patches) - 1:
        # At the end
        return success, message + "\n\n→ Last patch in series. Run 'end-session' when done."

    # Check for next patch with comments
    from .extractor import extract_comments

    for idx in range(current_index + 1, len(session.series_patches)):
        patch = session.series_patches[idx]
        try:
            base_url = session.series_url.rsplit('/', 1)[0]
            patch_url = f"{base_url}/{patch['change_number']}"
            comments = extract_comments(patch_url, include_resolved=False, include_code_context=False)
            if comments.threads:
                # Found next patch with comments - advance to it
                next_success, next_message = work_on_patch(session.series_url, patch['change_number'])
                if next_success:
                    return True, message + f"\n\n→ Auto-advanced to patch {patch['change_number']}\n\n" + next_message
                else:
                    return success, message + f"\n\n→ Next patch with comments: {patch['change_number']}"
        except Exception:
            continue

    return success, message + "\n\n→ No more patches with comments. Run 'end-session' when done."


def abort_patch() -> tuple[bool, str]:
    """Abort the current patch work.

    Returns:
        Tuple of (success, message)
    """
    manager = RebaseManager()
    return manager.abort_rebase()


def rebase_status() -> tuple[bool, str]:
    """Get the current rebase status.

    Returns:
        Tuple of (has_session, message)
    """
    manager = RebaseManager()
    return manager.get_status()


def end_session() -> tuple[bool, str]:
    """End the current rebase session.

    This clears the session state and returns to the current tip.
    Use this when you're done working on all patches in the series.

    Returns:
        Tuple of (success, message)
    """
    manager = RebaseManager()
    session = manager.load_session()
    if not session:
        return False, "No active rebase session"

    # Get current state for the summary
    current_commit = manager.get_current_commit()

    lines = []
    lines.append("=" * 70)
    lines.append("✓ Session ended")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"Series tip: {current_commit[:8] if current_commit else 'unknown'}")
    lines.append("")
    lines.append("To push all updated patches:")
    lines.append(f"  git push origin {current_commit[:8] if current_commit else 'HEAD'}:refs/for/master")
    lines.append("=" * 70)

    manager.clear_session()

    return True, "\n".join(lines)


def next_patch(with_comments: bool = False) -> tuple[bool, str]:
    """Move to the next patch in the series.

    Args:
        with_comments: If True, skip to next patch with unresolved comments

    Returns:
        Tuple of (success, message)
    """
    manager = RebaseManager()
    session = manager.load_session()
    if not session:
        return False, "No active rebase session. Start one with: gerrit work-on-patch <change> <url>"

    # Find current patch index
    current_index = -1
    for idx, patch in enumerate(session.series_patches):
        if patch['change_number'] == session.target_change:
            current_index = idx
            break

    if current_index == -1:
        return False, "Could not find current patch in series"

    # Find next patch
    if with_comments:
        # Need to check which patches have unresolved comments
        from .extractor import extract_comments

        next_patch_info = None
        for idx in range(current_index + 1, len(session.series_patches)):
            patch = session.series_patches[idx]
            try:
                # Construct URL for this patch
                base_url = session.series_url.rsplit('/', 1)[0]
                patch_url = f"{base_url}/{patch['change_number']}"
                comments = extract_comments(patch_url)
                if comments.threads:  # Has unresolved comments
                    next_patch_info = patch
                    break
            except Exception:
                continue

        if not next_patch_info:
            return False, "No more patches with unresolved comments in this series"

    else:
        # Just get the next patch
        if current_index >= len(session.series_patches) - 1:
            return False, "Already at the last patch in the series. Use 'end-session' when done."

        next_patch_info = session.series_patches[current_index + 1]

    # Call work_on_patch with the session URL and next change number
    return work_on_patch(session.series_url, next_patch_info['change_number'])


def get_session_url() -> Optional[str]:
    """Get the URL from the current session, if any.

    Returns:
        The series URL if there's an active session, None otherwise
    """
    manager = RebaseManager()
    session = manager.load_session()
    if session:
        return session.series_url
    return None


def get_session_info() -> Optional[dict]:
    """Get information about the current session.

    Returns:
        Dict with session info if active, None otherwise
    """
    manager = RebaseManager()
    session = manager.load_session()
    if session:
        return {
            'series_url': session.series_url,
            'target_change': session.target_change,
            'patches': session.series_patches,
        }
    return None
