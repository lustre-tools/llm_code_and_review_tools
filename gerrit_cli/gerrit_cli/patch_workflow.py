"""Patch workflow orchestration — module-level convenience functions.

Extracted from rebase.py as part of the module split. These functions
create a RebaseManager internally and delegate to it, providing a
simple functional API for patch series operations.
"""

from typing import Optional

from .tmux_integration import RebaseManager


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
        return success, message + "\n\n\u2192 Last patch in series. Run 'end-session' when done."

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
                    return True, message + f"\n\n\u2192 Auto-advanced to patch {patch['change_number']}\n\n" + next_message
                else:
                    return success, message + f"\n\n\u2192 Next patch with comments: {patch['change_number']}"
        except Exception:
            continue

    return success, message + "\n\n\u2192 No more patches with comments. Run 'end-session' when done."


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
    lines.append("\u2713 Session ended")
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
